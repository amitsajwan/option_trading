"""Probe: does Dhan's Expired Options Data API actually give us last-5-years,
minute-level option history (OHLC + OI + IV) we can train the ML pipeline on?

This is a VERIFICATION-ONLY script — it places no orders, writes no data.
It hits POST /v2/charts/rollingoption and answers, empirically:

  1. How far back does the data really go? (claim: "last 5 years")
  2. Is it true 1-minute granularity?
  3. Are IV and OI populated, or just OHLC+volume? (IV/OI are core ML features)
  4. How many ATM-relative strike offsets are served? (CALL + PUT)
  5. Is `spot` returned so we can reconstruct absolute strikes to match our
     2020-2024 parquet schema (timestamp, ohlc, volume, oi, strike, iv)?

Run on the VM where DHAN creds live (this repo's adapter uses the same vars):
    DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... python probe_dhan_history.py
Optional:
    --security-id N   underlying index securityId (default BANKNIFTY guess=25)
    --segment SEG     default IDX_I (index underlying)
Stdlib only (urllib) so it runs anywhere the dhan.py adapter runs.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

BASE = os.getenv("DHAN_API_BASE", "https://api.dhan.co/v2").rstrip("/")
ENDPOINT = "/charts/rollingoption"
ALL_FIELDS = ["open", "high", "low", "close", "volume", "oi", "iv"]


def _request(body: dict) -> tuple[int, dict]:
    token = os.getenv("DHAN_ACCESS_TOKEN", "")
    client = os.getenv("DHAN_CLIENT_ID", "")
    if not token:
        raise SystemExit("DHAN_ACCESS_TOKEN not set — run on the VM with creds.")
    req = urllib.request.Request(
        f"{BASE}{ENDPOINT}", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("access-token", token)
    req.add_header("client-id", client)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw or str(exc)}


def _body(sec_id: int, segment: str, frm: str, to: str, *,
          interval: str = "1", strike: str = "ATM", opt: str = "CALL",
          fields: list[str] | None = None) -> dict:
    return {
        "exchangeSegment": segment,
        "interval": interval,
        "securityId": sec_id,
        "instrument": "OPTIDX",
        "expiryFlag": "MONTH",
        "expiryCode": 0,            # 0 = nearest/active monthly expiry of that window
        "strike": strike,
        "drvOptionType": opt,
        "requiredData": fields or ALL_FIELDS,
        "fromDate": frm,
        "toDate": to,
    }


def _ce(payload: dict) -> dict:
    d = (payload or {}).get("data") or {}
    return d.get("ce") or {}


def _count(arr) -> int:
    return len(arr) if isinstance(arr, list) else 0


def _window(d0: date, days: int = 25) -> tuple[str, str]:
    return d0.isoformat(), (d0 + timedelta(days=days)).isoformat()


def test_history_floor(sec_id: int, segment: str) -> None:
    print("\n=== TEST 1+2: how far back, and is it minute-level? ===")
    today = date.today()
    # Probe one ~25-day window per year, newest to oldest.
    for years_back in range(0, 7):
        anchor = today.replace(year=today.year - years_back) - timedelta(days=40)
        frm, to = _window(anchor)
        status, payload = _request(_body(sec_id, segment, frm, to))
        ce = _ce(payload)
        ts = ce.get("timestamp") if isinstance(ce.get("timestamp"), list) else []
        n = _count(ts)
        if status != 200:
            print(f"  {frm}..{to}  HTTP {status}  {str(payload)[:120]}")
            continue
        gap = ""
        if n >= 3:
            diffs = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
            med = statistics.median(diffs)
            gap = f" median_gap={med}s ({'1-min' if med == 60 else f'{med/60:.0f}-min'})"
            first = datetime.utcfromtimestamp(ts[0]).isoformat()
            last = datetime.utcfromtimestamp(ts[-1]).isoformat()
            print(f"  {frm}..{to}  candles={n}  {first}..{last}{gap}")
        else:
            print(f"  {frm}..{to}  candles={n}  (empty/insufficient)")


def test_fields(sec_id: int, segment: str) -> None:
    print("\n=== TEST 3: which fields are actually populated (IV/OI = ML-critical) ===")
    frm, to = _window(date.today() - timedelta(days=45))
    status, payload = _request(_body(sec_id, segment, frm, to, fields=ALL_FIELDS + ["strike", "spot"]))
    if status != 200:
        print(f"  HTTP {status}: {str(payload)[:200]}")
        return
    ce = _ce(payload)
    for f in ALL_FIELDS + ["strike", "spot", "timestamp"]:
        arr = ce.get(f)
        n = _count(arr)
        sample = arr[:2] if isinstance(arr, list) and arr else arr
        flag = "OK " if n > 0 else "EMPTY"
        print(f"  {f:<10} {flag} n={n:<5} sample={sample}")


def test_strike_coverage(sec_id: int, segment: str) -> None:
    print("\n=== TEST 4: ATM-relative strike coverage (CALL & PUT) ===")
    frm, to = _window(date.today() - timedelta(days=45), days=10)
    for opt in ("CALL", "PUT"):
        served = []
        for off in range(0, 13):
            for s in ({"ATM"} if off == 0 else {f"ATM+{off}", f"ATM-{off}"}):
                status, payload = _request(_body(sec_id, segment, frm, to, strike=s, opt=opt))
                if status == 200 and _count(_ce(payload).get("close")) > 0:
                    served.append(s)
        print(f"  {opt}: {len(served)} offsets served -> {sorted(served)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--security-id", type=int, default=25,
                    help="underlying index securityId (BANKNIFTY guess=25; confirm via scrip master)")
    ap.add_argument("--segment", default="IDX_I")
    ap.add_argument("--only", choices=["floor", "fields", "strikes"], default=None)
    args = ap.parse_args()
    print(f"Dhan expired-options probe  base={BASE}  sec_id={args.security_id}  seg={args.segment}")
    if args.only in (None, "floor"):
        test_history_floor(args.security_id, args.segment)
    if args.only in (None, "fields"):
        test_fields(args.security_id, args.segment)
    if args.only in (None, "strikes"):
        test_strike_coverage(args.security_id, args.segment)
    print("\nVERDICT checklist: (a) earliest year with candles  (b) median_gap==60s "
          "(c) iv & oi non-empty  (d) strike offsets >= our training depth  (e) spot present.")


if __name__ == "__main__":
    main()
