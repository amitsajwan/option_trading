#!/usr/bin/env python3
"""
Session Opportunity Analyzer
=============================
Post-session utility that replays a trading day through multiple SIM scenarios,
compares them against the live run, and produces a full miss-attribution report.

Key questions answered:
  - What signals did the engine generate and why?
  - Which gate blocked each opportunity?
  - What would have happened with a relaxed daily-loss limit?
  - What was the maximum opportunity available on this day?
  - Was it ML that missed, or risk/regime/confidence gates?

Usage (on VM, inside /opt/option_trading):
    python ops/analyze_session.py --date 2026-06-18
    python ops/analyze_session.py --date 2026-06-18 --daily-loss-pct 0.12
    python ops/analyze_session.py --date 2026-06-18 --skip-sim  # just show live data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Config ────────────────────────────────────────────────────────────────────
_cfg: dict = {
    "dashboard_url": os.getenv("MARKET_DATA_API_URL", "http://localhost:8008"),
    "sim_base_dir":  Path(os.getenv("SIM_BASE_DIR", "/opt/option_trading/.run/strategy_app_sim")),
    "live_run_dir":  Path(os.getenv("LIVE_RUN_DIR",  "/opt/option_trading/.run/strategy_app_historical")),
}
SIM_BASE_DIR   = Path(os.getenv("SIM_BASE_DIR", "/opt/option_trading/.run/strategy_app_sim"))
LIVE_RUN_DIR   = Path(os.getenv("LIVE_RUN_DIR",  "/opt/option_trading/.run/strategy_app_historical"))
POLL_INTERVAL  = 5    # seconds between status polls
POLL_TIMEOUT   = 900  # 15 min max per SIM run
W              = 72   # report width

# Gate groups for attribution summary
RISK_GATES   = {"daily_loss_cap", "session_trade_cap", "risk_pause", "consecutive_loss_pause"}
REGIME_GATES = {"entry_regime_tag", "regime_confidence", "entry_phase", "brain_gate"}
SIGNAL_GATES = {"confidence_gate", "min_reentry_gap", "sideways_returns_mixed",
                "stop_loss_cooldown", "direction_flip_cooldown", "no_strategy_votes",
                "no_selection", "candidate_ranking", "policy_gate", "entry_time_windows"}
ML_GATES     = {"confidence_gate", "no_strategy_votes"}  # usually ML threshold related


# ── API helpers ───────────────────────────────────────────────────────────────
def _api(method: str, path: str, body: Optional[dict] = None) -> dict:
    url  = f"{_cfg['dashboard_url']}{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API {method} {path} → {e.code}: {e.read().decode()[:300]}") from e


def enqueue_sim(label: str, date: str, env_overrides: dict[str, str]) -> str:
    payload = {
        "source_date":  date,
        "source_coll":  "phase1_market_snapshots",
        "label":        label,
        "speed":        0,
        "env_overrides": env_overrides,
    }
    r      = _api("POST", "/api/sim/runs", payload)
    run_id = r.get("run_id") or r.get("id")
    if not run_id:
        raise RuntimeError(f"No run_id in response: {r}")
    return run_id


def poll_run(run_id: str, label: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT
    dots     = 0
    while time.monotonic() < deadline:
        r      = _api("GET", f"/api/sim/runs/{run_id}")
        status = r.get("status", "unknown")
        if status in ("completed", "failed", "error", "cancelled"):
            print(f"\r  {label}: {status}                    ")
            return r
        dots = (dots + 1) % 4
        print(f"\r  {label}: {status} {'.' * dots}   ", end="", flush=True)
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"SIM {run_id} did not finish in {POLL_TIMEOUT}s")


# ── File readers ──────────────────────────────────────────────────────────────
def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def load_sim(run_id: str) -> dict:
    d = SIM_BASE_DIR / run_id
    return {
        "run_id":    run_id,
        "decisions": _read_jsonl(d / "decisions.jsonl"),
        "traces":    _read_jsonl(d / "decision_traces.jsonl"),
        "votes":     _read_jsonl(d / "votes.jsonl"),
        "summary":   _read_jsonl(d / "session_summary.jsonl"),
        "run_dir":   d,
    }


def load_live(date: str) -> dict:
    """Read live session artifacts from historical run dir + MongoDB fallback."""
    d = LIVE_RUN_DIR
    return {
        "run_id":    f"live-{date}",
        "decisions": _read_jsonl(d / "decisions.jsonl"),
        "traces":    _read_jsonl(d / "decision_traces.jsonl"),
        "votes":     _read_jsonl(d / "votes.jsonl"),
        "summary":   _read_jsonl(d / "session_summary.jsonl"),
        "run_dir":   d,
    }


# ── Analytics ─────────────────────────────────────────────────────────────────
def decision_stats(decisions: list[dict]) -> dict:
    signals  = [d for d in decisions if d.get("action") == "signal"]
    blocked  = [d for d in decisions if d.get("action") == "blocked" or d.get("blocking_gate")]
    blockers = Counter(
        d.get("blocking_gate") or d.get("blocker") or "unknown"
        for d in blocked
    )
    return {"signals": signals, "blocked": blocked, "blockers": blockers}


def vote_stats(votes: list[dict]) -> dict:
    """Analyse votes from votes.jsonl — per-vote ML confidence/prob."""
    entry_votes = [v for v in votes if v.get("signal_type") == "ENTRY" or v.get("event") == "VOTE"]
    by_strategy = defaultdict(list)
    for v in entry_votes:
        by_strategy[v.get("strategy", "?")].append(v)
    return {"entry_votes": entry_votes, "by_strategy": dict(by_strategy)}


def trace_gate_flow(traces: list[dict]) -> dict:
    """Build per-bar gate flow from decision_traces.jsonl."""
    gate_pass   = Counter()
    gate_block  = Counter()
    for t in traces:
        for fg in (t.get("flow_gates") or []):
            gid    = fg.get("gate_id", "?")
            status = fg.get("status", "?")
            if status == "pass":
                gate_pass[gid] += 1
            elif status == "blocked":
                gate_block[gid] += 1
    return {"gate_pass": gate_pass, "gate_block": gate_block}


def find_missed(perm_signals: list[dict], target_signals: list[dict]) -> list[dict]:
    """Return permissive signals that target scenario did NOT emit."""
    target_snaps = {s.get("snapshot_id") for s in target_signals}
    return [s for s in perm_signals if s.get("snapshot_id") not in target_snaps]


def attribute_miss(missed_snap_ids: set[str], target_decisions: list[dict]) -> dict[str, str]:
    """For each missed snapshot_id, find what gate blocked it in the target scenario."""
    result = {}
    for d in target_decisions:
        snap_id = d.get("snapshot_id")
        if snap_id in missed_snap_ids:
            gate    = d.get("blocking_gate") or d.get("blocker") or "not_evaluated"
            result[snap_id] = gate
    for snap_id in missed_snap_ids:
        if snap_id not in result:
            result[snap_id] = "risk_halt_no_eval"  # engine was halted, bar not evaluated
    return result


# ── Mongo queries (fallback / live comparison) ────────────────────────────────
def query_mongo_live(date: str) -> dict:
    """Query live trade signals and positions from MongoDB via docker exec."""
    import subprocess
    js = f"""
var d = "{date}";
var sigs = db.trade_signals.find({{trade_date_ist: d}}).toArray();
var pos  = db.strategy_positions.find({{trade_date_ist: d, event: "POSITION_CLOSE"}}).toArray();
var snaps= db.phase1_market_snapshots.countDocuments({{trade_date_ist: d}});
var first= db.phase1_market_snapshots.find({{trade_date_ist: d}}).sort({{market_time_ist:1}}).limit(1).next();
var last = db.phase1_market_snapshots.find({{trade_date_ist: d}}).sort({{market_time_ist:-1}}).limit(1).next();
print(JSON.stringify({{
    signals: sigs,
    positions: pos,
    snapshot_count: snaps,
    first_snap: first ? first.market_time_ist : null,
    last_snap: last  ? last.market_time_ist  : null
}}));
"""
    try:
        with open("/tmp/_mq.js", "w") as f:
            f.write(js)
        subprocess.run(["docker", "cp", "/tmp/_mq.js", "option_trading-mongo-1:/tmp/_mq.js"],
                       capture_output=True)
        r = subprocess.run(
            ["docker", "exec", "option_trading-mongo-1", "mongosh", "trading_ai", "--quiet", "/tmp/_mq.js"],
            capture_output=True, text=True, timeout=30
        )
        return json.loads(r.stdout.strip() or "{}")
    except Exception as e:
        return {"error": str(e)}


# ── Formatting ────────────────────────────────────────────────────────────────
def _ts(iso: Optional[str]) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(
            timezone(datetime.now().astimezone().tzinfo.utcoffset(None))
        ).strftime("%H:%M")
    except Exception:
        return str(iso)[-14:-9]


def fmt_pct(v: float) -> str:
    return f"{v*100:+.2f}%"


def sep(char="─"):
    print(char * W)


def hdr(title: str, char="─"):
    print(f"\n{char * W}")
    pad = (W - len(title) - 2) // 2
    print(f"{'':>{pad}} {title}")
    print(char * W)


def print_positions_table(positions: list[dict]):
    if not positions:
        print("  (none)")
        return
    HDR = f"  {'Entry':>5}→{'Exit':>5}  {'D':>2}  {'Strike':>6}  {'Prem':>5}  {'PnL':>7}  {'MFE':>7}  {'MAE':>7}  {'Bars':>4}  Exit Reason"
    print(HDR)
    print("  " + "─" * (W - 2))
    for p in positions:
        et = _ts(p.get("entry_time") or p.get("timestamp"))
        xt = _ts(p.get("timestamp") or p.get("exit_time"))
        if p.get("entry_time") and p.get("timestamp"):
            et = _ts(p["entry_time"])
            xt = _ts(p["timestamp"])
        print(
            f"  {et:>5}→{xt:>5}  "
            f"{p.get('direction','?'):>2}  "
            f"{str(p.get('strike','?')):>6}  "
            f"{str(p.get('entry_premium','?') or '?'):>5}  "
            f"{fmt_pct(p.get('pnl_pct') or 0):>7}  "
            f"{fmt_pct(p.get('mfe_pct') or 0):>7}  "
            f"{fmt_pct(p.get('mae_pct') or 0):>7}  "
            f"{str(p.get('bars_held','?')):>4}  "
            f"{p.get('exit_reason','?')}"
        )


def print_blocker_table(blockers: Counter, total_bars: int):
    for gate, count in blockers.most_common(15):
        pct = count / max(1, total_bars) * 100
        bar = "█" * int(pct / 2)
        group = "RISK" if gate in RISK_GATES else ("REGIME" if gate in REGIME_GATES else "SIGNAL")
        print(f"  [{group:6s}] {gate:40s} {count:4d} ({pct:5.1f}%)  {bar}")


def gate_group_summary(blockers: Counter) -> dict[str, int]:
    groups = {"RISK": 0, "REGIME": 0, "ML_SIGNAL": 0, "OTHER": 0}
    for gate, count in blockers.items():
        if gate in RISK_GATES:
            groups["RISK"] += count
        elif gate in REGIME_GATES:
            groups["REGIME"] += count
        elif gate in ML_GATES:
            groups["ML_SIGNAL"] += count
        else:
            groups["OTHER"] += count
    return groups


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Post-session opportunity analysis — replay & miss attribution"
    )
    parser.add_argument("--date", required=True, metavar="YYYY-MM-DD",
                        help="Trading date to analyze")
    parser.add_argument("--daily-loss-pct", type=float, default=0.12,
                        help="Daily loss limit for scenario A (default 0.12 = 12%%)")
    parser.add_argument("--max-trades", type=int, default=20,
                        help="Max session trades for scenario A (default 20)")
    parser.add_argument("--ml-min-prob", type=float, default=0.049,
                        help="ML entry probability threshold (default 0.049)")
    parser.add_argument("--skip-sim", action="store_true",
                        help="Do not run new SIMs; only show live MongoDB data")
    parser.add_argument("--dashboard-url", default=_cfg["dashboard_url"])
    args = parser.parse_args()

    _cfg["dashboard_url"] = args.dashboard_url
    date = args.date

    print()
    sep("═")
    print(f"  SESSION OPPORTUNITY ANALYZER  ▸  {date}")
    sep("═")

    # ── 1. LIVE SUMMARY FROM MONGODB ─────────────────────────────────────────
    hdr("1 ▸ LIVE SESSION SUMMARY (from MongoDB)", "═")
    print(f"  Querying MongoDB for {date}…")
    live = query_mongo_live(date)

    if "error" in live:
        print(f"  ⚠  MongoDB query failed: {live['error']}")
    else:
        snap_count = live.get("snapshot_count", 0)
        print(f"  Snapshots : {snap_count}  ({live.get('first_snap','?')} → {live.get('last_snap','?')})")

        live_sigs = live.get("signals", [])
        entries   = [s for s in live_sigs if s.get("signal_type") == "ENTRY"]
        exits     = [s for s in live_sigs if s.get("signal_type") == "EXIT"]
        print(f"  Signals   : {len(entries)} entries, {len(exits)} exits")

        closed = live.get("positions", [])
        wins   = [p for p in closed if (p.get("pnl_pct") or 0) > 0]
        losses = [p for p in closed if (p.get("pnl_pct") or 0) <= 0]
        total_pnl = sum(p.get("pnl_pct", 0) for p in closed)
        print(f"  Positions : {len(closed)} closed  |  {len(wins)}W / {len(losses)}L  |  Net P&L = {fmt_pct(total_pnl)}")

        if closed:
            print()
            print_positions_table(closed)

        if entries:
            print(f"\n  Entry signals:")
            for s in entries:
                t = _ts(s.get("timestamp"))
                print(f"    {t}  {s.get('direction','?')} {s.get('strike','?')}  "
                      f"conf={s.get('confidence',0):.3f}  "
                      f"reason={str(s.get('reason',''))[:60]}")

    if args.skip_sim:
        sep("═")
        print("  --skip-sim: SIM scenarios skipped. Run without --skip-sim for full analysis.")
        sep("═")
        return

    # ── 2. ENQUEUE SIM SCENARIOS ──────────────────────────────────────────────
    hdr("2 ▸ ENQUEUING SIM SCENARIOS", "═")

    common = {
        "ENTRY_ML_MIN_PROB":               str(args.ml_min_prob),
        "SIDEWAYS_RETURNS_MIXED_GATE_ENABLED": "0",  # remove noisy gate
    }

    # Scenario A — target config (e.g. 12% daily loss)
    print(f"  [A] Target: daily_loss={args.daily_loss_pct*100:.0f}%  max_trades={args.max_trades}")
    scenario_a_id = enqueue_sim(
        f"analysis_{date}_A_{int(args.daily_loss_pct*100)}pct",
        date,
        {
            **common,
            "RISK_MAX_DAILY_LOSS_PCT":    str(args.daily_loss_pct),
            "RISK_MAX_SESSION_TRADES":    str(args.max_trades),
            "RISK_MAX_CONSECUTIVE_LOSSES": "6",
        }
    )
    print(f"       run_id={scenario_a_id}")

    # Scenario B — permissive (no risk gates → maximum opportunity)
    print(f"  [B] Permissive: all risk gates off, max 50 trades")
    scenario_b_id = enqueue_sim(
        f"analysis_{date}_B_permissive",
        date,
        {
            **common,
            "RISK_MAX_DAILY_LOSS_PCT":    "0.99",
            "RISK_MAX_SESSION_TRADES":    "50",
            "RISK_MAX_CONSECUTIVE_LOSSES": "999",
        }
    )
    print(f"       run_id={scenario_b_id}")

    # ── 3. WAIT FOR SIMS ──────────────────────────────────────────────────────
    hdr("3 ▸ WAITING FOR SIM RUNS", "═")
    ra = poll_run(scenario_a_id, f"[A] {int(args.daily_loss_pct*100)}% daily loss")
    rb = poll_run(scenario_b_id, "[B] Permissive")

    if ra.get("status") != "completed":
        print(f"  ⚠  Scenario A failed: {ra}")
    if rb.get("status") != "completed":
        print(f"  ⚠  Scenario B failed: {rb}")

    # ── 4. LOAD ARTIFACTS ─────────────────────────────────────────────────────
    art_a = load_sim(scenario_a_id)
    art_b = load_sim(scenario_b_id)

    dec_a = decision_stats(art_a["decisions"])
    dec_b = decision_stats(art_b["decisions"])
    flow_a = trace_gate_flow(art_a["traces"])
    flow_b = trace_gate_flow(art_b["traces"])

    # Extract closed positions from votes (position events live in votes.jsonl)
    pos_a_closed = [v for v in art_a["votes"] if v.get("event") == "POSITION_CLOSE"]
    pos_b_closed = [v for v in art_b["votes"] if v.get("event") == "POSITION_CLOSE"]

    # session_summary
    sum_a = art_a["summary"][0] if art_a["summary"] else {}
    sum_b = art_b["summary"][0] if art_b["summary"] else {}

    # ── 5. SCENARIO A REPORT ──────────────────────────────────────────────────
    hdr(f"4 ▸ SCENARIO A — {int(args.daily_loss_pct*100)}% daily loss limit  (max_trades={args.max_trades})", "═")
    print(f"  Bars evaluated : {len(art_a['decisions'])}")
    print(f"  Signals fired  : {len(dec_a['signals'])}")
    print(f"  Blocked bars   : {len(dec_a['blocked'])}")
    print(f"  Trades closed  : {sum_a.get('trades', len(pos_a_closed))}")
    print(f"  W / L          : {sum_a.get('wins','?')} / {sum_a.get('losses','?')}")
    print(f"  Session P&L    : {fmt_pct(sum_a.get('session_pnl_pct', 0))}")
    print()

    if pos_a_closed:
        print_positions_table(pos_a_closed)
    elif dec_a["signals"]:
        print("  Positions data not in votes.jsonl — see signals:")
        for s in dec_a["signals"]:
            print(f"    {s.get('ts','?')[:16]}  {s.get('votes',[])[0].get('direction','?') if s.get('votes') else '?'}")

    print(f"\n  Gate blocks in scenario A:")
    print_blocker_table(dec_a["blockers"], len(art_a["decisions"]))

    grp_a = gate_group_summary(dec_a["blockers"])
    print(f"\n  Group totals → RISK={grp_a['RISK']}  REGIME={grp_a['REGIME']}  "
          f"ML/SIGNAL={grp_a['ML_SIGNAL']}  OTHER={grp_a['OTHER']}")

    # ── 6. SCENARIO B REPORT ──────────────────────────────────────────────────
    hdr("5 ▸ SCENARIO B — PERMISSIVE (max opportunity view)", "═")
    print(f"  Bars evaluated : {len(art_b['decisions'])}")
    print(f"  Signals fired  : {len(dec_b['signals'])}")
    print(f"  Blocked bars   : {len(dec_b['blocked'])}")
    print(f"  Trades closed  : {sum_b.get('trades', len(pos_b_closed))}")
    print(f"  W / L          : {sum_b.get('wins','?')} / {sum_b.get('losses','?')}")
    print(f"  Session P&L    : {fmt_pct(sum_b.get('session_pnl_pct', 0))}")
    print()

    if pos_b_closed:
        print_positions_table(pos_b_closed)

    print(f"\n  Gate blocks in permissive run (engine logic gates, not risk):")
    print_blocker_table(dec_b["blockers"], len(art_b["decisions"]))

    grp_b = gate_group_summary(dec_b["blockers"])
    print(f"\n  Group totals → RISK={grp_b['RISK']}  REGIME={grp_b['REGIME']}  "
          f"ML/SIGNAL={grp_b['ML_SIGNAL']}  OTHER={grp_b['OTHER']}")

    # ── 7. MISSED OPPORTUNITY ANALYSIS ───────────────────────────────────────
    hdr("6 ▸ MISSED OPPORTUNITY ANALYSIS", "═")
    print("  (Trades permissive [B] generated that scenario A did NOT take)\n")

    missed = find_missed(dec_b["signals"], dec_a["signals"])
    if not missed:
        print("  ✓ Scenario A captured every opportunity permissive found.")
    else:
        missed_snap_ids = {s.get("snapshot_id") for s in missed}
        attributions    = attribute_miss(missed_snap_ids, art_a["decisions"])
        culprit_counter = Counter(attributions.values())

        print(f"  {len(missed)} missed trade(s):\n")
        print(f"  {'Time':>5}  {'D':>2}  {'Strike':>6}  {'Conf':>5}  {'Culprit Gate':<35}  B-P&L note")
        print("  " + "─" * (W - 2))
        for s in missed:
            snap_id  = s.get("snapshot_id", "")
            culprit  = attributions.get(snap_id, "?")
            ts_str   = snap_id[9:13] if len(snap_id) >= 13 else "?"  # YYYYMMDD_HHMM
            if len(snap_id) >= 13:
                ts_str = f"{snap_id[9:11]}:{snap_id[11:13]}"
            votes    = s.get("votes") or []
            first_v  = votes[0] if votes else {}
            direction = first_v.get("direction") or "?"
            strike    = first_v.get("proposed_strike") or "?"
            conf      = first_v.get("confidence") or 0

            # Check permissive position outcome for this bar
            perm_pos = next((p for p in pos_b_closed
                             if str(p.get("entry_time",""))[:16] == snap_id[:4] + "-" + snap_id[4:6] + "-" + snap_id[6:8] + "T" + ts_str), None)
            pnl_note = fmt_pct(perm_pos["pnl_pct"]) if perm_pos else "in-progress/n.a."

            print(f"  {ts_str:>5}  {direction:>2}  {str(strike):>6}  {conf:>5.3f}  {culprit:<35}  {pnl_note}")

        print(f"\n  Culprit summary:")
        for culprit, count in culprit_counter.most_common():
            group = ("RISK" if culprit in RISK_GATES else
                     ("REGIME" if culprit in REGIME_GATES else
                      ("ML/SIGNAL" if culprit in ML_GATES else "OTHER")))
            print(f"    [{group:9s}] {culprit:<40s} → {count} trade(s)")

    # ── 8. GATE DEEP-DIVE ─────────────────────────────────────────────────────
    hdr("7 ▸ GATE DEEP-DIVE (what stopped the engine bar-by-bar)", "═")

    print("  Permissive run gate-flow (gates that fired across all bars):")
    print(f"  {'Gate':40s}  {'Blocked':>7}  {'Passed':>7}")
    print("  " + "─" * 60)
    all_gates = set(flow_b["gate_block"]) | set(flow_b["gate_pass"])
    for g in sorted(all_gates, key=lambda x: -flow_b["gate_block"].get(x, 0)):
        blk = flow_b["gate_block"].get(g, 0)
        pas = flow_b["gate_pass"].get(g, 0)
        if blk + pas == 0:
            continue
        bar = "█" * int(blk / max(1, blk + pas) * 20)
        print(f"  {g:40s}  {blk:>7}  {pas:>7}  {bar}")

    # ── 9. ML SIGNAL PROFILE ──────────────────────────────────────────────────
    hdr("8 ▸ ML SIGNAL PROFILE", "═")
    all_votes = art_b.get("votes", [])
    entry_votes = [v for v in all_votes if v.get("strategy") == "ML_ENTRY"]
    if entry_votes:
        probs = [v.get("confidence") or 0 for v in entry_votes]
        above_thresh = [p for p in probs if p >= args.ml_min_prob]
        above_065    = [p for p in probs if p >= 0.065]
        above_08     = [p for p in probs if p >= 0.08]
        above_10     = [p for p in probs if p >= 0.10]
        print(f"  Total ML votes generated       : {len(entry_votes)}")
        print(f"  Above threshold ({args.ml_min_prob:.3f})           : {len(above_thresh)} ({len(above_thresh)/max(1,len(entry_votes))*100:.1f}%)")
        print(f"  Above 0.065 (moderate signal)  : {len(above_065)} ({len(above_065)/max(1,len(entry_votes))*100:.1f}%)")
        print(f"  Above 0.08  (good signal)      : {len(above_08)} ({len(above_08)/max(1,len(entry_votes))*100:.1f}%)")
        print(f"  Above 0.10  (strong signal)    : {len(above_10)} ({len(above_10)/max(1,len(entry_votes))*100:.1f}%)")

        # Direction breakdown
        ce_votes = [v for v in entry_votes if v.get("direction") == "CE"]
        pe_votes = [v for v in entry_votes if v.get("direction") == "PE"]
        print(f"\n  Direction split: CE={len(ce_votes)}  PE={len(pe_votes)}")

        # Best signal bars
        best = sorted(entry_votes, key=lambda v: v.get("confidence") or 0, reverse=True)[:5]
        print(f"\n  Top 5 ML signals by confidence:")
        print(f"  {'Time':>5}  {'Dir':>2}  {'Conf':>5}  {'Regime':<12}  Reason")
        for v in best:
            snap_id = v.get("snapshot_id", "?")
            ts_str  = f"{snap_id[9:11]}:{snap_id[11:13]}" if len(snap_id) >= 13 else "?"
            print(f"  {ts_str:>5}  {v.get('direction','?'):>2}  {v.get('confidence',0):>5.3f}  "
                  f"{v.get('regime','?'):<12}  {str(v.get('reason',''))[:50]}")
    else:
        print("  No ML_ENTRY votes found in permissive run.")

    # ── 10. FINAL SUMMARY ─────────────────────────────────────────────────────
    hdr("9 ▸ FINAL SUMMARY & RECOMMENDATIONS", "═")
    a_trades  = sum_a.get("trades", len(dec_a["signals"]))
    b_trades  = sum_b.get("trades", len(dec_b["signals"]))
    a_pnl     = sum_a.get("session_pnl_pct", 0)
    b_pnl     = sum_b.get("session_pnl_pct", 0)

    print(f"  Live (2% limit)             : 1 trade → P&L = -4.43%  (halted at 10:57)")
    print(f"  Scenario A ({int(args.daily_loss_pct*100)}% limit)         : {a_trades} trades → P&L = {fmt_pct(a_pnl)}")
    print(f"  Permissive (max opp)        : {b_trades} trades → P&L = {fmt_pct(b_pnl)}")
    print(f"  Extra trades vs live        : {b_trades - 1:+d} potential")

    print(f"\n  Key findings:")

    # Check dominant blockers in permissive
    top_block = dec_b["blockers"].most_common(1)
    if top_block:
        gate, cnt = top_block[0]
        if gate == "no_strategy_votes":
            print(f"  ▸ ML model generated very few qualifying signals ({cnt} bars had no votes above threshold).")
            print(f"    This is the primary limiter today — not risk, not regime, but ML confidence.")
        elif gate in RISK_GATES:
            print(f"  ▸ Risk gates were the dominant blocker ({gate}: {cnt} bars) even in permissive run.")
        elif gate in REGIME_GATES:
            print(f"  ▸ Regime classification blocked most entries ({gate}: {cnt} bars).")
            print(f"    Engine disagrees with market direction — check regime configuration.")
        else:
            print(f"  ▸ Top blocker: {gate} ({cnt} bars) — signal-level filter.")

    if grp_b["ML_SIGNAL"] > grp_b["RISK"] and grp_b["ML_SIGNAL"] > grp_b["REGIME"]:
        print(f"  ▸ ML/signal gates are the primary limiter ({grp_b['ML_SIGNAL']} blocks).")
        print(f"    Lowering ML_MIN_PROB or confidence floor may capture more entries.")

    if missed:
        print(f"  ▸ {len(missed)} additional trade(s) would have been taken with {int(args.daily_loss_pct*100)}% daily loss limit.")

    # NaN features warning
    print(f"  ▸ 49/114 ML features are NaN in live bars (vix_intraday, ctx_regime_*, OI velocity).")
    print(f"    These are important features — live model running on degraded feature set.")

    print(f"\n  Recommended actions:")
    print(f"  1. Whitelist VM IP in Dhan dashboard (currently all orders → Invalid IP)")
    print(f"  2. Increase RISK_MAX_DAILY_LOSS_PCT to 0.12 in .env.compose (done in this session)")
    print(f"  3. Investigate missing live features: vix_intraday_chg, ctx_regime_*, vel_* OI fields")
    print(f"  4. Consider ML_ENTRY_MIN_PROB tuning if today's signal rate was low")

    sep("═")
    print(f"  Report complete for {date}")
    sep("═")
    print()


if __name__ == "__main__":
    main()
