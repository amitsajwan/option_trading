#!/usr/bin/env python3
"""Single-trade postmortem probe: human-readable summary + raw dump.

Pulls a position from MongoDB, reconstructs the autopsy verdict, looks up the
actual 5-min futures move after entry, and shows the direction signals that were
live at the time.

Usage (copy to container, then run):
  # Look up a specific position (full id or 8-char prefix):
  python /tmp/probe_single_trade.py --pos fce59da2

  # List all positions for a date:
  python /tmp/probe_single_trade.py --date 2026-06-19

  # Both (filter list then probe one):
  python /tmp/probe_single_trade.py --date 2026-06-19 --pos fce59da2

  # Inside the mongo container (no pymongo needed):
  #   docker cp ops/gcp/probe_single_trade.py <mongo>:/tmp/
  #   docker exec <mongo> python3 /tmp/probe_single_trade.py --date 2026-06-19
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Optional

try:
    from pymongo import MongoClient
except ImportError:
    print("pymongo required — run inside the mongo or dashboard container", file=sys.stderr)
    sys.exit(2)

# Loss tag codes stored in decision_metrics (see deterministic_rule_engine.py)
_TAG_CODES = {
    1.0: "cost_miss",
    2.0: "exit_miss",
    3.0: "direction_miss",
    4.0: "entry_miss",
    5.0: "noise",
}
_TAG_NAMES = {v: k for k, v in _TAG_CODES.items()}


# ── serialisation ─────────────────────────────────────────────────────────────

def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonable(v) for v in o]
    if isinstance(o, datetime):
        return o.isoformat()
    try:
        json.dumps(o)
        return o
    except (TypeError, ValueError):
        return str(o)


def _dump(label: str, doc: Any) -> None:
    print(f"\n===== {label} =====")
    if doc is None:
        print("  (none found)")
        return
    print(json.dumps(_jsonable(doc), indent=2, default=str))


# ── autopsy reconstruction ────────────────────────────────────────────────────

def _reconstruct_autopsy(pos: dict) -> dict:
    """Reconstruct the autopsy verdict from persisted position fields.

    Uses the same logic as reflection.autopsy() without importing from strategy_app
    (so this script works inside the mongo container with no app deps).
    """
    dm = pos.get("decision_metrics") or {}

    # Try stored code first (requires engine fix to be deployed)
    code = dm.get("reflection_tag_code")
    if code and float(code) in _TAG_CODES:
        return {
            "tag": _TAG_CODES[float(code)],
            "is_loss": bool(dm.get("reflection_is_loss", 0)),
            "needs_reasoning": bool(dm.get("reflection_needs_reasoning", 0)),
            "overpaid": bool(dm.get("reflection_overpaid", 0)),
            "cost_to_edge": dm.get("reflection_cost_to_edge"),
            "source": "stored",
        }

    # Reconstruct from position numbers
    net = float(pos.get("pnl_pct") or 0.0)
    cost = abs(float(pos.get("cost_frac") or dm.get("cost_frac") or 0.013))
    gross = net + cost
    mfe = float(pos.get("mfe_pct") or 0.0)
    target = abs(float(pos.get("target_pct") or 0.40))
    stop = abs(float(pos.get("stop_loss_pct") or 0.20))
    mfe_to_target = (mfe / target) if target > 0 else None

    is_loss = net < 0.0
    if not is_loss:
        return {"tag": None, "is_loss": False, "needs_reasoning": False, "source": "reconstructed"}

    if gross >= 0.0 and net < 0.0:
        return {"tag": "cost_miss", "is_loss": True, "needs_reasoning": False, "source": "reconstructed"}
    if mfe_to_target is not None and mfe_to_target >= 0.80:
        return {"tag": "exit_miss", "is_loss": True, "needs_reasoning": False, "source": "reconstructed"}
    if mfe_to_target is not None and mfe_to_target <= 0.25:
        return {"tag": "direction_miss", "is_loss": True, "needs_reasoning": False, "source": "reconstructed"}
    small = stop > 0 and abs(net) <= 0.20 * stop
    return {
        "tag": "noise",
        "is_loss": True,
        "needs_reasoning": not small,
        "source": "reconstructed",
    }


# ── actual move lookup ────────────────────────────────────────────────────────

def _actual_move(db: Any, snapshot_id: str, lookahead_bars: int = 5) -> Optional[float]:
    """Return |fut_close(entry+N bars) - fut_close(entry)| in points."""
    snap = db["phase1_market_snapshots"].find_one({"snapshot_id": snapshot_id})
    if not snap:
        return None
    entry_price = (
        snap.get("payload", {}).get("snapshot", {}).get("futures_bar", {}).get("fut_close")
        or snap.get("futures_bar", {}).get("fut_close")
    )
    if not entry_price:
        return None

    ts = snap.get("timestamp")
    trade_date = snap.get("trade_date_ist") or snap.get("trade_date")
    if not ts or not trade_date:
        return None

    future_snaps = list(
        db["phase1_market_snapshots"]
        .find({"trade_date_ist": trade_date, "timestamp": {"$gt": ts}})
        .sort("timestamp", 1)
        .limit(lookahead_bars + 2)
    )
    if len(future_snaps) < lookahead_bars:
        return None
    fut_snap = future_snaps[lookahead_bars - 1]
    fut_price = (
        fut_snap.get("payload", {}).get("snapshot", {}).get("futures_bar", {}).get("fut_close")
        or fut_snap.get("futures_bar", {}).get("fut_close")
    )
    if not fut_price or not entry_price:
        return None
    return round(abs(float(fut_price) - float(entry_price)), 1)


# ── direction signal extraction ───────────────────────────────────────────────

def _direction_signals(pos: dict, trace: Optional[dict]) -> dict:
    """Pull direction signal values — first from position top-level fields, then trace."""
    # Position doc carries the most reliable values (written at entry)
    result = {
        "entry_prob": pos.get("ml_entry_prob"),
        "direction_prob_up": pos.get("ml_direction_up_prob"),
        "ml_ce_prob": pos.get("ml_ce_prob"),
        "ml_pe_prob": pos.get("ml_pe_prob"),
        "regime": pos.get("decision_reason_code"),
    }
    if not trace:
        return result
    payload = trace.get("payload") or {}
    tr = payload.get("trace") or {}
    direction = tr.get("direction_model") or tr.get("direction") or {}
    raw = payload.get("raw_signals") or {}
    result.update({
        "direction_voted": direction.get("voted_direction") or direction.get("direction"),
        "vwap_signal": raw.get("vwap_signal") or raw.get("price_vs_vwap"),
        "pcr": raw.get("pcr"),
        "momentum_15m": raw.get("momentum_15m"),
    })
    return result


# ── summary printer ───────────────────────────────────────────────────────────

def _fmt(v: Any, pct: bool = False, pts: bool = False) -> str:
    if v is None:
        return "?"
    try:
        f = float(v)
        if pct:
            return f"{f*100:+.2f}%"
        if pts:
            return f"{f:.1f}pt"
        return f"{f:.4f}"
    except (TypeError, ValueError):
        return str(v)


def _print_summary(pos: dict, trace: Optional[dict], db: Any) -> None:
    dm = pos.get("decision_metrics") or {}
    ap = _reconstruct_autopsy(pos)
    signals = _direction_signals(pos, trace)

    # Derive entry market time — POSITION_CLOSE has exit bar in market_time_ist,
    # so pull the POSITION_OPEN event to get the real entry bar time.
    trade_date = pos.get("trade_date_ist") or pos.get("trade_date") or ""
    open_doc = db["strategy_positions"].find_one(
        {"position_id": pos.get("position_id"), "event": "POSITION_OPEN"}
    )
    entry_mt = (open_doc or {}).get("market_time_ist") or pos.get("market_time_ist") or ""

    entry_snap_id = pos.get("entry_snapshot_id") or pos.get("snapshot_id")
    if not entry_snap_id and trade_date and entry_mt:
        entry_snap_id = trade_date.replace("-", "") + "_" + entry_mt.replace(":", "")[:4]
    actual_move = _actual_move(db, entry_snap_id) if entry_snap_id else None

    net = pos.get("pnl_pct")
    mfe = pos.get("mfe_pct")
    mae = pos.get("mae_pct")
    gross = (float(net or 0) + abs(float(pos.get("cost_frac") or dm.get("cost_frac") or 0.013)))

    print("\n" + "=" * 60)
    print("TRADE POSTMORTEM")
    print("=" * 60)
    print(f"  Position  : {pos.get('position_id', '?')}")
    print(f"  Date      : {pos.get('trade_date_ist') or pos.get('trade_date', '?')}")
    print(f"  Entry     : {entry_mt or pos.get('market_time_ist', '?')}  "
          f"{pos.get('direction', '?')}  strike={pos.get('strike', '?')}  "
          f"prem={pos.get('entry_premium', '?')}")
    exit_mt = pos.get("exit_time") or pos.get("market_time_ist") or "?"
    print(f"  Exit      : {exit_mt}  reason={pos.get('exit_reason', '?')}")
    print(f"  Bars held : {pos.get('bars_held', '?')}")
    print()
    print(f"  Net P&L   : {_fmt(net, pct=True)}   (gross {_fmt(gross, pct=True)})")
    print(f"  MFE       : {_fmt(mfe, pct=True)}   MAE: {_fmt(mae, pct=True)}")
    print()
    print(f"  Entry ML prob  : {_fmt(signals.get('entry_prob'))}")
    print(f"  CE prob        : {_fmt(signals.get('ml_ce_prob'))}   "
          f"PE prob: {_fmt(signals.get('ml_pe_prob'))}")
    print(f"  Dir up prob    : {_fmt(signals.get('direction_prob_up'))}")
    if signals.get("direction_voted"):
        print(f"  Direction voted: {signals['direction_voted']}")
    if signals.get("vwap_signal") is not None:
        print(f"  VWAP signal    : {signals['vwap_signal']}")
    if signals.get("pcr") is not None:
        print(f"  PCR            : {signals['pcr']}")
    print(f"  Regime         : {signals.get('regime', '?')}")
    print()
    if actual_move is not None:
        label = f"{actual_move}pt"
        flag = "  ✓ move happened" if actual_move >= 100 else "  (no big move)"
        print(f"  Actual 5-bar futures move: {label}{flag}")
    else:
        print(f"  Actual 5-bar futures move: (snapshot '{entry_snap_id}' not found)")
    print()
    tag = ap.get("tag")
    tag_str = tag.upper() if tag else "WIN / FLAT"
    print(f"  AUTOPSY   : {tag_str}  [{ap.get('source', '?')}]")
    if ap.get("needs_reasoning"):
        print(f"  ⚠  Ambiguous — would escalate to LLM autopsy")
    if dm.get("reflection_overpaid") or ap.get("overpaid"):
        cte = dm.get("reflection_cost_to_edge") or ap.get("cost_to_edge")
        print(f"  ⚠  Overpaid: cost_to_edge={_fmt(cte)}")
    print("=" * 60)


# ── listing by date ───────────────────────────────────────────────────────────

def _list_positions_for_date(db: Any, date: str) -> list[dict]:
    """Return one doc per position (prefer POSITION_CLOSE; fallback to latest event)."""
    coll = db["strategy_positions"]
    # Get all POSITION_CLOSE events for the date
    closes = list(coll.find(
        {"event": "POSITION_CLOSE", "$or": [{"trade_date_ist": date}, {"trade_date": date}]}
    ).sort("entry_time", 1))
    if closes:
        return closes
    # Fallback: deduplicate by position_id keeping the latest event
    all_docs = list(coll.find(
        {"$or": [{"trade_date_ist": date}, {"trade_date": date}]}
    ).sort("timestamp", 1))
    seen: dict[str, dict] = {}
    for d in all_docs:
        seen[d.get("position_id", str(d["_id"]))] = d
    return list(seen.values())


def _print_date_list(docs: list[dict]) -> None:
    if not docs:
        print("  No positions found for this date.")
        return
    print(f"\n{'POS_ID':12}  {'ENTRY':8}  {'DIR':4}  {'STRIKE':12}  {'P&L':8}  {'EXIT_REASON':20}  TAG")
    print("-" * 90)
    for p in docs:
        autopsy = _reconstruct_autopsy(p)
        pid = str(p.get("position_id") or "")[:12]
        entry = str(p.get("entry_time") or "")[-8:]
        direction = str(p.get("direction") or "")[:4]
        strike = str(p.get("strike") or "")[:12]
        net = p.get("pnl_pct")
        pnl = f"{float(net)*100:+.2f}%" if net is not None else "?"
        reason = str(p.get("exit_reason") or "")[:20]
        tag = (autopsy.get("tag") or "win")[:15]
        print(f"  {pid:12}  {entry:8}  {direction:4}  {strike:12}  {pnl:8}  {reason:20}  {tag}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Single-trade postmortem probe")
    ap.add_argument("--pos", default=None, help="position id (full or 8-char prefix)")
    ap.add_argument("--date", default=None, help="trade date YYYY-MM-DD (lists all positions)")
    ap.add_argument("--db", default=os.getenv("MONGO_DB", "trading_ai"))
    ap.add_argument("--url", default=os.getenv("MONGO_URL", "mongodb://mongo:27017"))
    ap.add_argument("--raw", action="store_true", help="also print raw JSON documents")
    args = ap.parse_args()

    if not args.pos and not args.date:
        ap.error("provide --pos <id>, --date <YYYY-MM-DD>, or both")

    db = MongoClient(args.url)[args.db]

    # Date listing
    if args.date and not args.pos:
        docs = _list_positions_for_date(db, args.date)
        print(f"\nPositions for {args.date}: {len(docs)} found")
        _print_date_list(docs)
        return

    # Single position probe
    pfx = args.pos

    def _find_one(coll_name: str, *fields: str) -> tuple[Optional[dict], Optional[str]]:
        if coll_name not in db.list_collection_names():
            return None, None
        coll = db[coll_name]
        for fld in fields:
            doc = coll.find_one({fld: {"$regex": f"^{pfx}"}})
            if doc:
                return doc, fld
        return None, None

    # 1. Position — prefer POSITION_CLOSE (has final pnl/mfe/exit_reason)
    coll = db["strategy_positions"]
    pos = coll.find_one(
        {"position_id": {"$regex": f"^{pfx}"}, "event": "POSITION_CLOSE"}
    )
    if pos is None:
        # Fallback: any event for this position (e.g. still open)
        pos = coll.find_one({"position_id": {"$regex": f"^{pfx}"}})
    fld = "position_id"

    if not pos and args.date:
        docs = _list_positions_for_date(db, args.date)
        for d in docs:
            if str(d.get("position_id", "")).startswith(pfx):
                pos, fld = d, "position_id"
                break

    if not pos:
        print(f"Position '{pfx}' not found in strategy_positions.")
        if args.date:
            docs = _list_positions_for_date(db, args.date)
            print(f"\nAll positions for {args.date}:")
            _print_date_list(docs)
        sys.exit(1)

    full_id = pos.get("position_id") or pos.get("id") or pos.get("trade_id")

    # 2. Decision trace — match on signal_id (the entry trace)
    sig_id = pos.get("signal_id")
    trace = None
    if sig_id:
        trace = db["strategy_decision_traces"].find_one(
            {"$or": [{"signal_id": sig_id}, {"snapshot_id": {"$regex": f"^{pos.get('trade_date_ist','').replace('-','')}_{pos.get('market_time_ist','').replace(':','')[:4]}"}}]}
        )
    if trace is None:
        trace, _ = _find_one("strategy_decision_traces", "position_id", "signal_id", "id", "trace_id")

    # 3. Human-readable summary
    _print_summary(pos, trace, db)

    if not args.raw:
        return

    # 4. Raw dumps (opt-in)
    print(f"\n[matched position on field '{fld}']")
    _dump("POSITION", pos)
    _dump(f"TRACE[strategy_decision_traces]", trace)

    # Trade signals
    for sc in ("trade_signals", "strategy_votes"):
        sig, sfld = _find(sc, "signal_id", "position_id", "id", "trade_id")
        if sig:
            print(f"\n[matched signal in {sc} on '{sfld}']")
            _dump(f"SIGNAL[{sc}]", sig)
            break

    # Strategy votes around this position
    if "strategy_votes" in db.list_collection_names() and full_id:
        cur = list(db["strategy_votes"].find({"$or": [
            {"position_id": full_id},
            {"position_id": {"$regex": f"^{pfx}"}},
            {"signal_id": {"$regex": f"^{pfx}"}},
        ]}).limit(10))
        if cur:
            print(f"\n[strategy_votes: {len(cur)} docs]")
            for i, v in enumerate(cur):
                _dump(f"VOTE[{i}]", v)


if __name__ == "__main__":
    main()
