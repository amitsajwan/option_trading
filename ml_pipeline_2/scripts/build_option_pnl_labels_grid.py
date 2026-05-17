"""Grid expansion — adds 4 more recipes for diversity testing.

Original v1 recipes (already in option_pnl_labels_v1):
    ATM_CE_9, ATM_PE_9, ATM_CE_15, ATM_PE_15

Grid additions explored here (option_pnl_labels_grid):
    ATM_CE_5    — short hold (faster turnover, less theta)
    ATM_PE_5
    OTM1_CE_15  — one step out-of-money (cheaper premium, more leverage on move)
    OTM1_PE_15

Driver delegates to the same per-date processing as build_option_pnl_labels.py
but with the expanded recipe list. Writes to a separate output root so the
v1 labels stay clean.

Usage:
    python -m ml_pipeline_2.scripts.build_option_pnl_labels_grid \\
      --date-from 2020-08-01 --date-to 2024-10-31 \\
      --out /opt/option_trading/.data/ml_pipeline/parquet_data/option_pnl_labels_grid
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

import pandas as pd

from ml_pipeline_2.labeling.option_pnl import LabelContract, Recipe
from ml_pipeline_2.scripts.build_option_pnl_labels import daterange, process_date
from ml_pipeline_2.scripts.option_pnl_smoke import DEFAULT_FLAT_ROOT, DEFAULT_OPTIONS_ROOT


def make_recipes_grid() -> list[Recipe]:
    """Extended recipe set covering shorter holds + OTM-1 strike picks.

    Why these specific recipes:
      - ATM_CE_5 / ATM_PE_5: shorter hold means less theta decay; if the
        intraday momentum signal is real, a 5-bar window should capture
        the bulk of it with less noise.
      - OTM1_CE_15 / OTM1_PE_15: OTM options have higher leverage per
        rupee of premium but are more sensitive to spot direction. If
        the model's direction signal is real, OTM should amplify it; if
        the signal is just regime-fitting, OTM should die first.

    Held-aside recipes (not in this grid, deferred to later):
      - OTM2, ITM1 — multiply blast radius, defer to focused experiments.
      - Hold 30/60 bars — would push into late-day risk + harder
        labeler constraints around HARD_CLOSE.
    """
    return [
        Recipe(id="ATM_CE_5",   option_type="CE", strike_offset_steps=0, max_hold_bars=5,
               stop_pct_of_premium=0.15, target_pct_of_premium=0.20),
        Recipe(id="ATM_PE_5",   option_type="PE", strike_offset_steps=0, max_hold_bars=5,
               stop_pct_of_premium=0.15, target_pct_of_premium=0.20),
        Recipe(id="OTM1_CE_15", option_type="CE", strike_offset_steps=1, max_hold_bars=15,
               stop_pct_of_premium=0.30, target_pct_of_premium=0.50),
        Recipe(id="OTM1_PE_15", option_type="PE", strike_offset_steps=1, max_hold_bars=15,
               stop_pct_of_premium=0.30, target_pct_of_premium=0.50),
    ]


def run(args) -> int:
    flat_root = Path(args.flat_root)
    options_root = Path(args.options_root)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    start = pd.Timestamp(args.date_from)
    end = pd.Timestamp(args.date_to)
    dates = daterange(start, end)
    print(f"=== Option-P&L labels driver (grid v2) ===")
    print(f"date range: {start.date()} → {end.date()}  ({len(dates)} business days)")
    print(f"out: {out_root}")

    recipes = make_recipes_grid()
    contract = LabelContract()
    print(f"recipes: {[r.id for r in recipes]}")
    print()

    status_counts: Counter[str] = Counter()
    total_labels = 0
    total_skipped = 0
    t0 = time.time()
    for i, d in enumerate(dates):
        try:
            m = process_date(
                trade_date=d, flat_root=flat_root, options_root=options_root,
                out_root=out_root, recipes=recipes, contract=contract,
                overwrite=args.overwrite,
            )
            status = m["status"]
            status_counts[status] += 1
            if status == "ok":
                total_labels += m.get("labels_emitted", 0)
                total_skipped += m.get("skipped_count", 0)
            if (i + 1) % 20 == 0 or i == len(dates) - 1:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                print(f"  [{i+1}/{len(dates)}] {d.date()} status={status}  "
                      f"running_total_labels={total_labels}  rate={rate:.1f} d/s")
        except Exception as exc:  # noqa: BLE001
            status_counts["uncaught_exception"] += 1
            print(f"  [{i+1}/{len(dates)}] {d.date()} EXCEPTION: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    print()
    print(f"=== Grid label-build complete ===")
    print(f"total labels emitted: {total_labels:,}")
    print(f"total skipped:        {total_skipped:,}")
    print(f"status breakdown:")
    for s, c in status_counts.most_common():
        print(f"  {s:<30s} {c}")
    return 0 if status_counts.get("ok", 0) > 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Build expanded-grid option-P&L labels")
    p.add_argument("--date-from", required=True)
    p.add_argument("--date-to", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--flat-root", default=str(DEFAULT_FLAT_ROOT))
    p.add_argument("--options-root", default=str(DEFAULT_OPTIONS_ROOT))
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
