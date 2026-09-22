#!/usr/bin/env python3
"""Capital-pool reconciliation -- crash-safety net for the shared capital pool.

Born 2026-09-21 alongside contracts_app/capital_pool.py: the pool's
reserved_by_instrument counters are maintained incrementally (reserve on
entry, release on close/cancel). A container that crashes between a
successful reservation and the matching release would leak that
reservation forever if nothing ever corrected it. This script recomputes
"what SHOULD be reserved right now" from the one thing this repo already
treats as canonical -- each instrument's positions.jsonl (JSONL is
system-of-record; Mongo, including the pool document itself, is a
best-effort derived store) -- and corrects the live pool document if it's
drifted.

"Should be reserved" per instrument = replay that instrument's
positions.jsonl in order; POSITION_OPEN opens a position, POSITION_CLOSE or
POSITION_CANCELLED closes it (see strategy_app/logging/signal_logger.py's
log_position_cancel, added the same day as this script specifically so a
cancelled phantom position doesn't stay invisible to this replay). Whatever
position_id is still open at EOF is currently reserved, at
entry_premium * lots * lot_size -- the same capital_at_risk formula
RiskManager already uses (strategy_app/risk/manager.py update()).

Reads every instrument's positions.jsonl from one host directory (every
container already bind-mounts the same host ./.run -> /app/.run, so this
script needs no container access at all) and writes one corrective update
to the shared Mongo pool document if drift exceeds a small epsilon.

Run as a systemd timer on the VM (same operational pattern as the existing
daily Dhan-token-refresh timer) every ~2 minutes during market hours --
deliberately NOT embedded in any of the 5 peer instrument containers, since
none of them is a natural "owner" of cross-instrument state.

Usage:
    python3 ops/capital_pool_reconcile.py                  # read-only report, no write
    python3 ops/capital_pool_reconcile.py --apply           # correct drift if found
    python3 ops/capital_pool_reconcile.py --apply --run-dir /path/to/.run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_RUN_DIR = Path("/opt/option_trading/.run")
EPSILON_RS = 1.0


def _run_dir_name_for(instrument: str) -> str:
    """e.g. BANKNIFTY -> 'strategy_app', NIFTY -> 'strategy_app_nifty'.

    Delegates to the same Namespace.run_dir_for() the live containers use,
    then takes just the directory name -- avoids hardcoding the naming
    convention a second time in this script.
    """
    from contracts_app import resolve_namespace
    return resolve_namespace("live", instrument=instrument).run_dir_for().name


def _resolve_lot_size(instrument: str) -> int:
    from contracts_app import get_instrument
    return int(get_instrument(instrument).lot_size)


def compute_should_be_reserved(run_dir: Path, instrument: str) -> tuple[float, int]:
    """Returns (amount_reserved, open_position_count) for one instrument,
    by replaying its positions.jsonl. open_position_count should never
    exceed 1 (PositionTracker enforces one position at a time) -- if it
    does, that's a real bug worth surfacing loudly, not silently summing.
    """
    path = run_dir / _run_dir_name_for(instrument) / "positions.jsonl"
    if not path.exists():
        return 0.0, 0

    lot_size = _resolve_lot_size(instrument)
    open_positions: dict[str, dict] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = rec.get("event")
            pos_id = rec.get("position_id")
            if not pos_id:
                continue
            if event == "POSITION_OPEN":
                open_positions[pos_id] = rec
            elif event in ("POSITION_CLOSE", "POSITION_CANCELLED"):
                open_positions.pop(pos_id, None)

    total = 0.0
    for rec in open_positions.values():
        entry_premium = float(rec.get("entry_premium") or 0.0)
        lots = int(rec.get("lots") or 1)
        total += entry_premium * lots * lot_size
    return total, len(open_positions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the correction (default: read-only report)")
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR, help="Host .run directory (default: %(default)s)")
    args = parser.parse_args()

    from contracts_app import known_instruments
    from contracts_app import capital_pool

    pool_doc = capital_pool.status()
    if pool_doc is None:
        print("[capital_pool_reconcile] pool document does not exist yet (CAPITAL_POOL_ENABLED never turned on) -- nothing to do.")
        return 0

    total_capital = float(pool_doc.get("total", 0.0))
    live_reserved = dict(pool_doc.get("reserved_by_instrument", {}))

    should_be: dict[str, float] = {}
    warnings: list[str] = []
    for instrument in known_instruments():
        amount, open_count = compute_should_be_reserved(args.run_dir, instrument)
        should_be[instrument] = amount
        if open_count > 1:
            warnings.append(f"{instrument}: {open_count} open positions in positions.jsonl -- expected at most 1")

    print(f"[capital_pool_reconcile] pool total={total_capital:.2f}")
    print(f"{'instrument':<12} {'live_reserved':>14} {'should_be':>14} {'drift':>10}")
    max_drift = 0.0
    for instrument in known_instruments():
        live = float(live_reserved.get(instrument, 0.0))
        truth = should_be[instrument]
        drift = live - truth
        max_drift = max(max_drift, abs(drift))
        print(f"{instrument:<12} {live:>14.2f} {truth:>14.2f} {drift:>10.2f}")

    for w in warnings:
        print(f"[capital_pool_reconcile] WARNING: {w}")

    if max_drift <= EPSILON_RS:
        print(f"[capital_pool_reconcile] OK -- max drift {max_drift:.2f} within epsilon ({EPSILON_RS}).")
        return 0

    print(f"[capital_pool_reconcile] DRIFT DETECTED: max drift {max_drift:.2f} exceeds epsilon ({EPSILON_RS}).")
    if not args.apply:
        print("[capital_pool_reconcile] read-only mode (pass --apply to correct) -- no write performed.")
        return 1

    capital_pool.force_set_reserved(should_be, total=total_capital)
    print("[capital_pool_reconcile] corrected -- pool document updated to match positions.jsonl truth.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
