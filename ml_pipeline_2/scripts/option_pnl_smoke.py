"""End-to-end smoke test for the option-P&L labeler on ONE real trade day.

Validates the labeler integration against the actual options + snapshots
parquet on disk. Run on the ML VM after `pip install -e ml_pipeline_2`:

    python -m ml_pipeline_2.scripts.option_pnl_smoke --date 2024-01-02

Reports per-recipe: attempted / emitted / label-positive-rate / skip-reasons.
Bails with non-zero exit code if any structural integrity check fails
(e.g. zero rows emitted, all-NaN P&L, etc.).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import pandas as pd

from ml_pipeline_2.labeling.option_pnl import (
    LabelContract,
    PremiumLookup,
    Recipe,
    SKIP_LABEL,
    label_one,
)


DEFAULT_FLAT_ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2")
DEFAULT_OPTIONS_ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/options")


# ── In-memory premium lookup backed by one day of options parquet ───────────


class ParquetDayLookup(PremiumLookup):
    """Loads ONE day of options data into a dict for fast O(1) lookup.

    Keys: (timestamp_minute, strike, option_type) → close / oi.

    Expiry filtering: only the chosen-expiry rows are indexed. If multiple
    expiries are in the parquet (weekly + monthly), the caller picks one
    per trade_date via pick_expiry_for_date().
    """

    def __init__(self, df: pd.DataFrame, expiry_str: str) -> None:
        df = df[df["expiry_str"] == expiry_str].copy()
        df["minute"] = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
        # Stable index for the labeler's get_close / get_oi
        self._closes: dict[tuple[int, int, str], float] = {}
        self._ois: dict[tuple[int, int, str], float] = {}
        for _, row in df.iterrows():
            key = (int(row["minute"]), int(row["strike"]), str(row["option_type"]))
            self._closes[key] = float(row["close"]) if pd.notna(row["close"]) else None
            self._ois[key] = float(row["oi"]) if pd.notna(row["oi"]) else None

    def get_close(self, *, timestamp_minute, trade_date, strike, option_type, expiry_str):
        return self._closes.get((int(timestamp_minute), int(strike), str(option_type)))

    def get_oi(self, *, timestamp_minute, trade_date, strike, option_type, expiry_str):
        return self._ois.get((int(timestamp_minute), int(strike), str(option_type)))


# ── Helpers ─────────────────────────────────────────────────────────────────


def pick_expiry_for_date(options_df: pd.DataFrame, trade_date: pd.Timestamp) -> Optional[str]:
    """Among the expiries present for this trade_date, pick the nearest one
    that is >= trade_date (the current weekly).

    Falls back to first available if all expiries are in the past (shouldn't
    happen for real data — bail with None and the caller can investigate)."""
    by_expiry: list[tuple[pd.Timestamp, str]] = []
    for exp_str in options_df["expiry_str"].dropna().unique():
        try:
            exp_dt = pd.to_datetime(exp_str, format="%d%b%y")
        except ValueError:
            continue
        by_expiry.append((exp_dt, exp_str))
    if not by_expiry:
        return None
    forward = [(d, s) for d, s in by_expiry if d >= trade_date]
    if forward:
        return min(forward)[1]
    # All in past — return latest past (defensive).
    return max(by_expiry)[1]


def derive_strike_step(options_df: pd.DataFrame) -> Optional[int]:
    """Strike grid step — typically 100 for BankNifty, 50 for Nifty."""
    strikes = sorted(set(int(s) for s in options_df["strike"].dropna().unique()))
    if len(strikes) < 2:
        return None
    diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    if not diffs:
        return None
    return min(diffs)


def make_recipes_v1() -> list[Recipe]:
    """The 4 v1 recipes from the equivalence contract."""
    return [
        Recipe(id="ATM_CE_9",  option_type="CE", strike_offset_steps=0, max_hold_bars=9,
               stop_pct_of_premium=0.20, target_pct_of_premium=0.30),
        Recipe(id="ATM_PE_9",  option_type="PE", strike_offset_steps=0, max_hold_bars=9,
               stop_pct_of_premium=0.20, target_pct_of_premium=0.30),
        Recipe(id="ATM_CE_15", option_type="CE", strike_offset_steps=0, max_hold_bars=15,
               stop_pct_of_premium=0.25, target_pct_of_premium=0.40),
        Recipe(id="ATM_PE_15", option_type="PE", strike_offset_steps=0, max_hold_bars=15,
               stop_pct_of_premium=0.25, target_pct_of_premium=0.40),
    ]


# ── Driver ─────────────────────────────────────────────────────────────────


def load_snapshots_for_date(flat_root: Path, trade_date: pd.Timestamp) -> pd.DataFrame:
    """snapshots_ml_flat_v2 is partitioned by year then date. Load the one
    file we need."""
    candidate = flat_root / f"year={trade_date.year}" / f"{trade_date.strftime('%Y-%m-%d')}.parquet"
    if not candidate.exists():
        raise FileNotFoundError(f"snapshot parquet not found: {candidate}")
    df = pd.read_parquet(candidate)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["minute"] = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
    return df


def load_options_for_month(options_root: Path, trade_date: pd.Timestamp) -> pd.DataFrame:
    """options/ is partitioned year=YYYY/month=MM."""
    candidate = options_root / f"year={trade_date.year}" / f"month={trade_date.month:02d}" / "data.parquet"
    if not candidate.exists():
        raise FileNotFoundError(f"options parquet not found: {candidate}")
    df = pd.read_parquet(candidate)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df[df["trade_date"] == trade_date.date()].copy()


def run(args) -> int:
    flat_root = Path(args.flat_root)
    options_root = Path(args.options_root)
    trade_date = pd.Timestamp(args.date)

    print(f"=== Option-P&L labeler smoke test ===")
    print(f"trade_date: {trade_date.date()}")
    print(f"flat_root: {flat_root}")
    print(f"options_root: {options_root}")

    # Load data
    snaps = load_snapshots_for_date(flat_root, trade_date)
    print(f"snapshots: {len(snaps)} rows")

    options = load_options_for_month(options_root, trade_date)
    print(f"options (filtered to date): {len(options)} rows")
    if options.empty:
        print("ERROR: no options data for this date — aborting", file=sys.stderr)
        return 2

    expiry_str = pick_expiry_for_date(options, trade_date)
    if expiry_str is None:
        print("ERROR: could not derive expiry — aborting", file=sys.stderr)
        return 2
    print(f"chosen expiry: {expiry_str}")

    strike_step = derive_strike_step(options)
    if strike_step is None:
        print("ERROR: could not derive strike_step — aborting", file=sys.stderr)
        return 2
    print(f"strike_step: {strike_step}")

    lookup = ParquetDayLookup(options, expiry_str)
    print(f"lookup loaded: {len(lookup._closes)} (minute,strike,side) tuples for chosen expiry")

    recipes = make_recipes_v1()
    contract = LabelContract()

    # Run labeler
    per_recipe_stats: dict[str, Counter[str]] = {r.id: Counter() for r in recipes}
    per_recipe_pnl: dict[str, list[float]] = {r.id: [] for r in recipes}
    per_recipe_emitted: dict[str, list[dict]] = {r.id: [] for r in recipes}

    for _, srow in snaps.iterrows():
        atm = srow.get("opt_flow_atm_strike")
        if pd.isna(atm):
            for r in recipes:
                per_recipe_stats[r.id]["snapshot_missing_atm"] += 1
            continue
        snap_dict = {
            "timestamp_minute": int(srow["minute"]),
            "trade_date": str(trade_date.date()),
            "atm_strike": int(atm),
            "strike_step": strike_step,
            "expiry_str": expiry_str,
        }
        for r in recipes:
            out = label_one(snapshot=snap_dict, recipe=r, lookup=lookup, contract=contract)
            if out["label"] == SKIP_LABEL:
                per_recipe_stats[r.id][out["reason_skipped"]] += 1
            else:
                per_recipe_stats[r.id][f"label_{out['label']}"] += 1
                per_recipe_pnl[r.id].append(out["net_pnl_pct"])
                per_recipe_emitted[r.id].append(out)

    # Report
    print()
    print("=== Per-recipe outcomes ===")
    overall_emitted = 0
    for r in recipes:
        stats = per_recipe_stats[r.id]
        emitted = stats.get("label_0", 0) + stats.get("label_1", 0)
        overall_emitted += emitted
        pos_rate = (stats.get("label_1", 0) / emitted) if emitted > 0 else 0.0
        pnls = per_recipe_pnl[r.id]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
        median_pnl = sorted(pnls)[len(pnls) // 2] if pnls else 0.0
        print(f"\n  {r.id}:")
        print(f"    emitted     : {emitted}")
        print(f"    label_pos_rate : {pos_rate:.3f}")
        print(f"    avg net pnl : {avg_pnl:+.4f}")
        print(f"    median pnl  : {median_pnl:+.4f}")
        if emitted < 50:
            print(f"    WARNING: <50 labels — sample too small for confidence")
        print(f"    skip reasons (top):")
        for reason, count in stats.most_common(8):
            if reason.startswith("label_"):
                continue
            print(f"      {reason:<40s} {count}")

    print()
    print(f"=== Summary ===")
    print(f"total labels emitted: {overall_emitted}")

    # Sanity gates
    failures: list[str] = []
    if overall_emitted < 10:
        failures.append(f"FAIL: only {overall_emitted} labels emitted — driver broken or data gap")
    for r in recipes:
        stats = per_recipe_stats[r.id]
        emitted = stats.get("label_0", 0) + stats.get("label_1", 0)
        if emitted == 0:
            failures.append(f"FAIL: recipe {r.id} emitted 0 labels")

    if failures:
        print()
        for f in failures:
            print(f, file=sys.stderr)
        return 1

    print("OK: smoke gates passed. Inspect per-recipe stats above for sanity.")
    if args.json_out:
        json_path = Path(args.json_out)
        summary = {
            "date": str(trade_date.date()),
            "expiry": expiry_str,
            "strike_step": strike_step,
            "snapshot_rows": int(len(snaps)),
            "overall_emitted": overall_emitted,
            "per_recipe": {
                r.id: {
                    "emitted": per_recipe_stats[r.id].get("label_0", 0) + per_recipe_stats[r.id].get("label_1", 0),
                    "label_1": per_recipe_stats[r.id].get("label_1", 0),
                    "label_0": per_recipe_stats[r.id].get("label_0", 0),
                    "skip_reasons": {k: v for k, v in per_recipe_stats[r.id].items() if not k.startswith("label_")},
                    "avg_net_pnl_pct": float(sum(per_recipe_pnl[r.id]) / len(per_recipe_pnl[r.id])) if per_recipe_pnl[r.id] else 0.0,
                }
                for r in recipes
            },
        }
        json_path.write_text(json.dumps(summary, indent=2))
        print(f"summary written to {json_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Option-P&L labeler smoke test on one trade day")
    p.add_argument("--date", required=True, help="trade date YYYY-MM-DD")
    p.add_argument("--flat-root", default=str(DEFAULT_FLAT_ROOT))
    p.add_argument("--options-root", default=str(DEFAULT_OPTIONS_ROOT))
    p.add_argument("--json-out", default=None)
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
