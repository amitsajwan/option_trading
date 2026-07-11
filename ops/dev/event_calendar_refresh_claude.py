#!/usr/bin/env python3
"""Event-calendar refresh via Claude Code headless (dev-box variant).

Same librarian contract as ops/gcp/event_calendar_refresh.py, different LLM
backend: instead of a Gemini API key (billing-blocked as of 2026-07-11), this
calls the locally installed Claude Code CLI (`claude -p` + WebSearch), which
runs on the user's Pro subscription. Because scheduled macro dates are known
weeks ahead, this runs WEEKLY from the dev box (Task Scheduler) and pushes the
validated file to the VM; the VM-side Gemini timer stays installed as a dormant
alternative (it SKIPs without a key).

Flow: pull current calendar from VM (preserve "manual": true entries) → ask
Claude with web search → validate hard (ISO dates, 45-day horizon, size caps)
→ atomic local write → scp to VM → Telegram summary. Any failure keeps the
existing file on both sides (stale beats absent).

Alerts: reads ALERT_TELEGRAM_TOKEN / ALERT_TELEGRAM_CHAT_ID from an untracked
`.env.alerts` at the repo root (optional — refresh works without it).
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, timedelta

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CAL = os.path.join(REPO, "config", "event_calendar.json")
ENV_ALERTS = os.path.join(REPO, ".env.alerts")
HORIZON_DAYS = 45
MAX_EVENTS = 40
CLAUDE_MODEL = os.getenv("CALENDAR_CLAUDE_MODEL", "sonnet")

GCLOUD_CONFIG = "trader-new"
VM = "trader-runtime-01"
ZONE = "asia-south1-b"
PROJECT = "trader-502012"
VM_CAL = "/opt/option_trading/config/event_calendar.json"


def _gcloud_env():
    env = dict(os.environ)
    env["CLOUDSDK_ACTIVE_CONFIG_NAME"] = GCLOUD_CONFIG
    return env


def _vm_ssh(cmd, timeout=90):
    return subprocess.run(
        ["gcloud", "compute", "ssh", VM, f"--zone={ZONE}", f"--project={PROJECT}",
         "--command", cmd],
        capture_output=True, text=True, timeout=timeout, env=_gcloud_env(), shell=(os.name == "nt"))


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
        f"List all SCHEDULED events in the next {HORIZON_DAYS} days from {today} "
        "that typically move Indian index markets (NIFTY/BANKNIFTY): RBI MPC "
        "meeting dates and decision day, US FOMC meeting dates and decision day, "
        "Indian Union Budget, Indian general/major-state election result days, "
        "and US CPI release dates. Use web search to verify dates. Reply with "
        'ONLY a JSON array, no prose, each item {"date":"YYYY-MM-DD","label":'
        '"short name"}. Use exchange-relevant dates (IST). If a meeting spans '
        "days, list each day."
    )
    proc = subprocess.run(
        ["claude", "-p", prompt, "--allowedTools", "WebSearch",
         "--model", CLAUDE_MODEL],
        capture_output=True, text=True, timeout=600, shell=(os.name == "nt"))
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise RuntimeError(f"claude call failed rc={proc.returncode}: "
                           f"{(proc.stderr or '')[:200]}")
    return proc.stdout, "claude-websearch"


def _parse_events(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in response")
    raw = json.loads(m.group(0))
    today = date.today()
    out, seen = [], set()
    for e in raw:
        try:
            d = date.fromisoformat(str(e.get("date", ""))[:10])
            label = str(e.get("label", "")).strip()[:60]
        except Exception:
            continue
        if not label or d < today or d > today + timedelta(days=HORIZON_DAYS):
            continue
        key = (d.isoformat(), label.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": d.isoformat(), "label": label, "source": "llm"})
    if not out:
        raise ValueError("0 valid events after validation")
    return out[:MAX_EVENTS]


def _pull_vm_calendar():
    """Current VM file is authoritative for manual entries (may be hand-edited there)."""
    r = _vm_ssh(f"cat {VM_CAL} 2>/dev/null || echo '{{}}'")
    try:
        return json.loads(r.stdout.strip() or "{}")
    except Exception:
        return {}


def _push_vm_calendar():
    r = subprocess.run(
        ["gcloud", "compute", "scp", CAL, f"{VM}:/tmp/event_calendar.json",
         f"--zone={ZONE}", f"--project={PROJECT}"],
        capture_output=True, text=True, timeout=120, env=_gcloud_env(), shell=(os.name == "nt"))
    if r.returncode != 0:
        raise RuntimeError(f"scp failed: {(r.stderr or '')[:200]}")
    # cp (not mv): the containers file-bind-mount this path, so the write must
    # reuse the existing inode — an inode swap leaves them reading a stale ghost.
    r = _vm_ssh(f"sudo cp /tmp/event_calendar.json {VM_CAL} && "
                f"rm -f /tmp/event_calendar.json && python3 -c "
                f"\"import json;d=json.load(open('{VM_CAL}'));"
                f"print('VM_OK',len(d.get('events',[])),d.get('refreshed_at'))\"")
    if "VM_OK" not in r.stdout:
        raise RuntimeError(f"VM verify failed: {(r.stdout + r.stderr)[:200]}")
    return r.stdout.strip()


def main():
    vm_doc = _pull_vm_calendar()
    existing = vm_doc if vm_doc.get("events") else (
        json.load(open(CAL)) if os.path.exists(CAL) else {"events": []})
    manual = [e for e in existing.get("events", []) if e.get("manual")]
    try:
        text, mode = _ask_claude()
        events = _parse_events(text)
    except Exception as exc:
        print(f"REFRESH FAILED ({exc}) — keeping existing calendar")
        _telegram(f"<b>EVENT CALENDAR</b> refresh failed ({str(exc)[:80]}) — "
                  f"using existing file ({len(existing.get('events', []))} events)")
        return 1
    merged = manual + [e for e in events
                       if not any(m["date"] == e["date"] for m in manual)]
    merged.sort(key=lambda e: e["date"])
    doc = {
        "_comment": ("Seller entry veto dates. AUTO-REFRESHED weekly by "
                     "ops/dev/event_calendar_refresh_claude.py (Claude headless); add "
                     "\"manual\": true to any entry you edit by hand and it will be "
                     "preserved."),
        "refreshed_at": date.today().isoformat(),
        "refresh_mode": mode,
        "events": merged,
    }
    tmp = CAL + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, CAL)
    try:
        vm_status = _push_vm_calendar()
    except Exception as exc:
        print(f"LOCAL OK but VM push failed: {exc}")
        _telegram(f"<b>EVENT CALENDAR</b> refreshed locally ({len(merged)} events) "
                  f"but VM push FAILED: {str(exc)[:80]}")
        return 1
    nxt = ", ".join(f"{e['date'][5:]} {e['label']}" for e in merged[:6])
    print(f"OK: {len(merged)} events ({mode}); {vm_status}; next: {nxt}")
    _telegram(f"<b>EVENT CALENDAR</b> refreshed ({mode}): {len(merged)} events, "
              f"pushed to VM.\nNext: {nxt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
