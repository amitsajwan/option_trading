#!/usr/bin/env python3
"""Premarket brief via Claude Code headless (dev-box, ~08:20 IST weekdays).

Librarian pattern (same contract as the event calendar): the LLM RETRIEVES
overnight facts with web search and writes a validated JSON brief; it makes
no trading decision and no gate reads it yet — shadow evidence first. Each
brief is appended to a local JSONL so its calls can be scored against what
the session actually did (see first30_check_claude.py) BEFORE anything is
allowed to act on it.

Outputs: config/premarket_brief.json (local + pushed to VM via inode-safe cp),
ops/dev/.premarket_history.jsonl (append), Telegram summary.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import date, datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BRIEF = os.path.join(REPO, "config", "premarket_brief.json")
HISTORY = os.path.join(os.path.dirname(__file__), ".premarket_history.jsonl")
ENV_ALERTS = os.path.join(REPO, ".env.alerts")
CLAUDE_MODEL = os.getenv("PREMARKET_CLAUDE_MODEL", "sonnet")


def _claude_bin():
    """Real .exe, not claude.cmd — batch wrappers re-parse args via cmd.exe,
    which eats pipes/quotes inside the prompt (bit us 2026-07-11)."""
    import shutil
    exe = os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules",
                       "@anthropic-ai", "claude-code", "bin", "claude.exe")
    return exe if os.path.exists(exe) else (shutil.which("claude") or "claude")

GCLOUD_CONFIG = "trader-new"
VM = "trader-runtime-01"
ZONE = "asia-south1-b"
PROJECT = "trader-502012"
# Runtime data dir (shared_run mount), NOT the git-tracked config/ — a daily
# push onto a tracked path leaves the VM worktree dirty and blocks every merge.
VM_BRIEF = "/opt/option_trading/.run/premarket_brief.json"

VOL_FLAGS = ("calm", "elevated", "high")


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


def _ask_claude():
    today = date.today().isoformat()
    prompt = (
        f"Today is {today} (IST). Produce a PREMARKET brief for Indian index "
        "options (NIFTY/BANKNIFTY) before the 09:15 IST open. Use web search. "
        "Report ONLY facts you can verify: 1) last US session S&P500 and "
        "Nasdaq % change; 2) GIFT Nifty level and implied gap % vs NIFTY's "
        "last close; 3) Brent crude and USDINR level; 4) up to 4 overnight "
        "headlines that matter for Indian banks/markets (one line each); "
        "5) scheduled market-moving events TODAY (RBI/FOMC/CPI/budget/results); "
        "6) an overall expected-volatility flag for today's Indian session: "
        "calm | elevated | high, with a one-line rationale. "
        "Reply with ONLY a JSON object: {\"date\":\"" + today + "\", "
        "\"us_spx_pct\":num|null, \"us_ndx_pct\":num|null, "
        "\"gift_nifty_gap_pct\":num|null, \"brent_usd\":num|null, "
        "\"usdinr\":num|null, \"headlines\":[str], \"today_events\":[str], "
        "\"vol_flag\":\"calm|elevated|high\", \"rationale\":str}"
    )
    proc = subprocess.run(
        [_claude_bin(), "-p", prompt,
         "--allowedTools", "WebSearch", "--model", CLAUDE_MODEL],
        capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise RuntimeError(f"claude call failed rc={proc.returncode}: "
                           f"{(proc.stderr or '')[:200]}")
    return proc.stdout


def _parse_brief(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in response")
    raw = json.loads(m.group(0))
    out = {"date": date.today().isoformat(),
           "generated_at": datetime.now().isoformat(timespec="seconds")}
    for k in ("us_spx_pct", "us_ndx_pct", "gift_nifty_gap_pct", "brent_usd", "usdinr"):
        v = raw.get(k)
        try:
            out[k] = round(float(v), 3) if v is not None else None
        except (TypeError, ValueError):
            out[k] = None
    out["headlines"] = [str(h).strip()[:160] for h in (raw.get("headlines") or [])][:4]
    out["today_events"] = [str(e).strip()[:80] for e in (raw.get("today_events") or [])][:6]
    flag = str(raw.get("vol_flag", "")).strip().lower()
    if flag not in VOL_FLAGS:
        raise ValueError(f"bad vol_flag {flag!r}")
    out["vol_flag"] = flag
    out["rationale"] = str(raw.get("rationale", "")).strip()[:240]
    # sanity bounds — a hallucinated gap of 40% must not ship
    if out["gift_nifty_gap_pct"] is not None and abs(out["gift_nifty_gap_pct"]) > 8:
        raise ValueError(f"implausible gap {out['gift_nifty_gap_pct']}")
    return out


def _push_vm():
    env = dict(os.environ)
    env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = GCLOUD_CONFIG
    r = subprocess.run(
        ["gcloud", "compute", "scp", BRIEF, f"{VM}:/tmp/premarket_brief.json",
         f"--zone={ZONE}", f"--project={PROJECT}"],
        capture_output=True, text=True, timeout=120, env=env, shell=(os.name == "nt"))
    if r.returncode != 0:
        raise RuntimeError(f"scp failed: {(r.stderr or '')[:200]}")
    # cp not mv: keep the inode (future-proof for file bind mounts)
    r = subprocess.run(
        ["gcloud", "compute", "ssh", VM, f"--zone={ZONE}", f"--project={PROJECT}",
         "--command", f"sudo cp /tmp/premarket_brief.json {VM_BRIEF} && rm -f /tmp/premarket_brief.json && echo VM_OK"],
        capture_output=True, text=True, timeout=90, env=env, shell=(os.name == "nt"))
    if "VM_OK" not in r.stdout:
        raise RuntimeError(f"VM write failed: {(r.stdout + r.stderr)[:200]}")


def main():
    if date.today().weekday() >= 5 and not os.getenv("PREMARKET_FORCE"):
        print("weekend — skip")
        return 0
    try:
        brief = _parse_brief(_ask_claude())
    except Exception as exc:
        print(f"BRIEF FAILED: {exc}")
        _telegram(f"<b>PREMARKET BRIEF</b> failed: {str(exc)[:100]}")
        return 1
    with open(BRIEF, "w") as fh:
        json.dump(brief, fh, indent=1)
    with open(HISTORY, "a") as fh:
        fh.write(json.dumps(brief) + "\n")
    try:
        _push_vm()
        vm_note = "pushed to VM"
    except Exception as exc:
        vm_note = f"VM push failed: {str(exc)[:60]}"
    gap = brief["gift_nifty_gap_pct"]
    lines = [
        f"<b>PREMARKET {brief['date']}</b>  vol: <b>{brief['vol_flag'].upper()}</b>",
        f"US: SPX {brief['us_spx_pct']:+.2f}% / NDX {brief['us_ndx_pct']:+.2f}%"
        if brief["us_spx_pct"] is not None and brief["us_ndx_pct"] is not None else "US: n/a",
        f"GIFT gap: {gap:+.2f}%" if gap is not None else "GIFT gap: n/a",
        brief["rationale"],
    ]
    if brief["today_events"]:
        lines.append("Today: " + "; ".join(brief["today_events"]))
    for h in brief["headlines"]:
        lines.append(f"• {h}")
    lines.append(f"({vm_note})")
    _telegram("\n".join(lines))
    print(f"OK vol={brief['vol_flag']} gap={gap} {vm_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
