#!/usr/bin/env python3
"""First-30-minutes check via Claude headless (dev-box, ~09:50 IST weekdays).

Closes the loop on the premarket brief: pulls the ACTUAL first 30 minutes of
index bars (09:15-09:45 IST) from the VM's ingestion API, computes gap/range/
direction deterministically, then asks Claude for a short read: did the
session open the way the brief expected, and what regime does the first half
hour suggest? Output is a Telegram note + a JSONL row pairing prediction vs
actuals so the brief's calibration can be SCORED over weeks before any gate
is allowed to consume it (librarian discipline: evidence first, wiring later).

The deterministic scoring fields are computed here in code; Claude only writes
the prose read. A Claude failure still logs+sends the deterministic numbers.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import date, datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BRIEF = os.path.join(REPO, "config", "premarket_brief.json")
HISTORY = os.path.join(os.path.dirname(__file__), ".first30_history.jsonl")
ENV_ALERTS = os.path.join(REPO, ".env.alerts")
CLAUDE_MODEL = os.getenv("PREMARKET_CLAUDE_MODEL", "sonnet")


def _claude_bin():
    """Real .exe, not claude.cmd — batch wrappers re-parse args via cmd.exe."""
    exe = os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules",
                       "@anthropic-ai", "claude-code", "bin", "claude.exe")
    return exe if os.path.exists(exe) else (shutil.which("claude") or "claude")

GCLOUD_CONFIG = "trader-new"
VM = "trader-runtime-01"
ZONE = "asia-south1-b"
PROJECT = "trader-502012"


def _read_env(path):
    d = {}
    if os.path.exists(path):
        for ln in open(path, encoding="utf-8", errors="surrogateescape"):
            ln = ln.strip()
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def _telegram(text):
    env = _read_env(ENV_ALERTS)
    tok, chat = env.get("ALERT_TELEGRAM_TOKEN"), env.get("ALERT_TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def _vm_first30(instrument, today):
    """First 09:15-09:45 index bars via the ingestion container's day API."""
    env = dict(os.environ)
    env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = GCLOUD_CONFIG
    # Helper lives as a FILE on the VM (ops/gcp/first30_fetch.py) — inline
    # python through double-ssh quoting breaks (2026-07-11, again).
    r = subprocess.run(
        ["gcloud", "compute", "ssh", VM, f"--zone={ZONE}", f"--project={PROJECT}",
         "--command",
         "sudo docker run --rm --network option_trading_default "
         "-v /opt/option_trading/ops/gcp/first30_fetch.py:/f.py "
         f"python:3.12-slim python /f.py {instrument} {today}"],
        capture_output=True, text=True, timeout=420, env=env, shell=(os.name == "nt"))
    line = (r.stdout or "").strip().splitlines()
    bars = json.loads(line[-1]) if line else []
    if not bars:
        raise RuntimeError(f"no bars for {instrument} ({(r.stderr or '')[:120]})")
    closes = [float(b.get("close") or 0) for b in bars if b.get("close")]
    highs = [float(b.get("high") or 0) for b in bars if b.get("high")]
    lows = [float(b.get("low") or 0) for b in bars if b.get("low")]
    o = float(bars[0].get("open") or closes[0])
    last = closes[-1]
    return {
        "open": o, "at_0945": last,
        "first30_pct": round((last - o) / o * 100, 3),
        "range_pct": round((max(highs) - min(lows)) / o * 100, 3),
        "bars": len(bars),
    }


def _ask_claude(brief, actuals):
    prompt = (
        "You are reviewing an Indian index-options premarket brief against the "
        "actual first 30 minutes of trading (09:15-09:45 IST). Brief:\n"
        + json.dumps(brief) + "\nActual first-30-min stats:\n" + json.dumps(actuals)
        + "\nIn <=5 short lines: 1) did the open match the brief's gap/vol "
        "expectation? 2) gap held or faded? 3) what regime does the first half "
        "hour suggest (quiet range / trending / volatile chop)? 4) anything the "
        "brief missed (you may web-search for a 09:15-09:45 IST news event). "
        "Do NOT give trade or direction advice. Plain text, no markdown."
    )
    proc = subprocess.run(
        [_claude_bin(), "-p", prompt,
         "--allowedTools", "WebSearch", "--model", CLAUDE_MODEL],
        capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise RuntimeError("claude call failed")
    return proc.stdout.strip()[:900]


def main():
    today = os.getenv("FIRST30_TEST_DATE") or date.today().isoformat()
    if date.today().weekday() >= 5 and not os.getenv("PREMARKET_FORCE"):
        print("weekend — skip")
        return 0
    brief = {}
    if os.path.exists(BRIEF):
        try:
            brief = json.load(open(BRIEF))
        except Exception:
            pass
    if brief.get("date") != today:
        brief = {"date": today, "note": "no brief for today"}
    actuals = {}
    for inst in ("BANKNIFTY", "NIFTY"):
        try:
            actuals[inst] = _vm_first30(inst, today)
        except Exception as exc:
            actuals[inst] = {"error": str(exc)[:120]}
    # Deterministic calibration fields (scored later, in code, not by the LLM)
    nifty = actuals.get("NIFTY") or {}
    rec = {
        "date": today, "checked_at": datetime.now().isoformat(timespec="seconds"),
        "brief_vol_flag": brief.get("vol_flag"),
        "brief_gap_pct": brief.get("gift_nifty_gap_pct"),
        "actual_first30_pct": nifty.get("first30_pct"),
        "actual_range_pct": nifty.get("range_pct"),
        "actuals": actuals,
    }
    try:
        rec["llm_read"] = _ask_claude(brief, actuals)
    except Exception as exc:
        rec["llm_read"] = f"(claude failed: {str(exc)[:80]})"
    with open(HISTORY, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    bn = actuals.get("BANKNIFTY") or {}
    lines = [f"<b>FIRST-30 CHECK {today}</b>"]
    for name, a in (("BN", bn), ("NIFTY", nifty)):
        if "error" in a:
            lines.append(f"{name}: data unavailable")
        elif a:
            lines.append(f"{name}: {a['first30_pct']:+.2f}% (range {a['range_pct']:.2f}%)")
    lines.append(rec["llm_read"])
    _telegram("\n".join(lines))
    print("OK", json.dumps({k: rec[k] for k in ('brief_vol_flag', 'actual_first30_pct', 'actual_range_pct')}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
