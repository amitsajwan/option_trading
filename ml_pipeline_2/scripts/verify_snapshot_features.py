"""Snapshot feature completeness audit.

Loads snapshots_ml_flat_v2 (or v3 if available) and reports null rates,
value ranges, and anomalies for every feature group in the contract.
Filters to 11:30 rows only — the only rows where velocity features are populated.

Run on ML VM:
    /opt/option_trading/.venv/bin/python \
        ml_pipeline_2/scripts/verify_snapshot_features.py \
        [--parquet-root /path/to/parquet_data] [--start 2023-01-01] [--end 2024-12-31]
        [--all-rows]  # include non-11:30 rows too (velocity cols will be mostly NaN)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# ── contract ──────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_FILE = _REPO_ROOT / "snapshot_app" / "contracts" / "snapshot_ml_flat" / "feature_groups.json"

# Feature groups that are ONLY populated at 11:30 (velocity / morning context)
VELOCITY_GROUPS = {
    "velocity_oi", "velocity_pcr", "velocity_price", "velocity_iv",
    "velocity_volume", "morning_context",
}

# Expected integer / flag columns (0/1 or -1/0/1)
FLAG_PREFIXES = ("ctx_is_", "ctx_regime_", "ctx_gap_up", "ctx_gap_down",
                 "ctx_am_gap_filled", "ctx_am_trend", "ctx_am_reversal",
                 "ctx_am_oi_direction", "ctx_am_vwap_side", "ctx_am_breakout_confirmed",
                 "vel_pcr_trend_direction")


def _resolve_parquet_root(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("OPTION_TRADING_PARQUET_ROOT", "").strip()
    if env:
        return Path(env)
    # Common VM path
    fallback = Path("/opt/option_trading/.data/parquet_data")
    if fallback.exists():
        return fallback
    raise SystemExit(
        "Cannot find parquet root. Set OPTION_TRADING_PARQUET_ROOT or pass --parquet-root."
    )


def _load_flat(flat_root: Path, start: str, end: str, columns: List[str]) -> pd.DataFrame:
    """Load parquet rows from hive-partitioned year=YYYY/data.parquet layout."""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    years = range(start_ts.year, end_ts.year + 1)
    frames = []
    for y in years:
        for f in sorted(flat_root.glob(f"year={y}/*.parquet")):
            avail = set(pq.read_schema(f).names)
            load_cols = [c for c in columns if c in avail] + ["trade_date", "timestamp"]
            df = pd.read_parquet(f, columns=list(dict.fromkeys(load_cols)))
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            mask = (df["trade_date"] >= start_ts) & (df["trade_date"] <= end_ts)
            if mask.any():
                frames.append(df[mask])
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _load_contract() -> Dict[str, List[str]]:
    if not _CONTRACT_FILE.exists():
        raise SystemExit(f"Contract file not found: {_CONTRACT_FILE}")
    with _CONTRACT_FILE.open() as fh:
        raw = json.load(fh)
    return {grp: data["columns"] for grp, data in raw["groups"].items()}


def _all_columns(groups: Dict[str, List[str]]) -> List[str]:
    seen: List[str] = []
    for cols in groups.values():
        for c in cols:
            if c not in seen:
                seen.append(c)
    return seen


def _flag_col(name: str) -> bool:
    return any(name.startswith(p) for p in FLAG_PREFIXES) or name in {
        "ctx_am_gap_filled", "ctx_gap_up", "ctx_gap_down",
    }


def _group_stats(df: pd.DataFrame, group: str, cols: List[str]) -> dict:
    total = len(df)
    row: dict = {"group": group, "n_rows": total}
    col_stats = []
    for c in cols:
        if c not in df.columns:
            col_stats.append({"col": c, "status": "MISSING"})
            continue
        s = pd.to_numeric(df[c], errors="coerce")
        null_pct = round(100.0 * s.isna().sum() / max(total, 1), 1)
        valid = s.dropna()
        if len(valid) == 0:
            col_stats.append({
                "col": c, "null_pct": null_pct, "status": "ALL_NULL",
                "min": None, "max": None, "mean": None, "std": None,
            })
            continue
        mn = float(valid.min())
        mx = float(valid.max())
        mean = float(valid.mean())
        std = float(valid.std())
        issues = []
        if null_pct > 50:
            issues.append(f"high_null={null_pct}%")
        if _flag_col(c):
            unique_vals = sorted(valid.unique().tolist())
            invalid_flags = [v for v in unique_vals if v not in (-1, 0, 1)]
            if invalid_flags:
                issues.append(f"flag_out_of_range={invalid_flags[:3]}")
        else:
            if not np.isfinite(mn) or not np.isfinite(mx):
                issues.append("inf_values")
            if std == 0.0 and len(valid) > 5:
                issues.append("zero_variance")
        col_stats.append({
            "col": c,
            "null_pct": null_pct,
            "status": "OK" if not issues else ("WARN: " + "; ".join(issues)),
            "min": round(mn, 4),
            "max": round(mx, 4),
            "mean": round(mean, 4),
            "std": round(std, 4),
        })
    row["columns"] = col_stats
    ok = sum(1 for c in col_stats if c.get("status", "").startswith("OK"))
    warn = sum(1 for c in col_stats if "WARN" in c.get("status", ""))
    missing = sum(1 for c in col_stats if c.get("status") in ("MISSING", "ALL_NULL"))
    row["ok"] = ok
    row["warn"] = warn
    row["missing"] = missing
    return row


def _print_group(stats: dict, verbose: bool) -> None:
    g = stats["group"]
    n = stats["n_rows"]
    ok = stats["ok"]
    warn = stats["warn"]
    miss = stats["missing"]
    total_cols = ok + warn + miss
    tag = "✓" if warn == 0 and miss == 0 else ("!" if miss > 0 else "~")
    print(f"\n[{tag}] {g:30s}  rows={n:,d}  cols={total_cols}  ok={ok}  warn={warn}  missing={miss}")
    if verbose or warn > 0 or miss > 0:
        for cs in stats["columns"]:
            status = cs.get("status", "?")
            if not verbose and status == "OK":
                continue
            col = cs["col"]
            if status == "MISSING":
                print(f"     {col:45s}  !! MISSING from parquet")
                continue
            null_pct = cs.get("null_pct", "?")
            mn = cs.get("min", "?")
            mx = cs.get("max", "?")
            mean = cs.get("mean", "?")
            print(f"     {col:45s}  null={null_pct:5.1f}%  min={mn}  max={mx}  mean={mean}  {status}")


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parquet-root", default=None)
    p.add_argument("--start", default="2022-01-01", help="Start date (inclusive)")
    p.add_argument("--end", default="2024-12-31", help="End date (inclusive)")
    p.add_argument(
        "--dataset",
        default=None,
        help="Parquet sub-folder name: snapshots_ml_flat_v2 (default), snapshots_ml_flat_v3, snapshots_ml_flat",
    )
    p.add_argument(
        "--all-rows",
        action="store_true",
        help="Include all intraday rows (not just 11:30). Velocity columns will show high null%.",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print stats for every column, not just issues.",
    )
    p.add_argument(
        "--groups",
        nargs="+",
        default=None,
        help="Only audit these feature groups (space-separated group names from feature_groups.json).",
    )
    args = p.parse_args(argv)

    root = _resolve_parquet_root(args.parquet_root)

    # pick the best available dataset
    if args.dataset:
        dataset_name = args.dataset
    else:
        for candidate in ("snapshots_ml_flat_v3", "snapshots_ml_flat_v2", "snapshots_ml_flat"):
            if (root / candidate).exists():
                dataset_name = candidate
                break
        else:
            raise SystemExit(f"No snapshots_ml_flat* dataset found under {root}")

    flat_root = root / dataset_name
    print(f"Dataset : {flat_root}")
    print(f"Window  : {args.start} → {args.end}")

    groups = _load_contract()
    if args.groups:
        unknown = [g for g in args.groups if g not in groups]
        if unknown:
            print(f"Warning: unknown groups (ignored): {unknown}", file=sys.stderr)
        groups = {g: v for g, v in groups.items() if g in args.groups}

    all_cols = _all_columns(groups)
    load_cols = ["trade_date", "timestamp"] + all_cols

    print(f"Loading {len(all_cols)} feature columns …")
    df = _load_flat(flat_root, args.start, args.end, load_cols)

    if df.empty:
        raise SystemExit("No data loaded — check date range and parquet root path.")

    print(f"Total rows loaded: {len(df):,d}  dates: {df['trade_date'].nunique():,d}")

    # Identify 11:30 rows by timestamp minute
    df["_ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["_minute"] = df["_ts"].dt.hour * 60 + df["_ts"].dt.minute

    midday_mask = df["_minute"] == (11 * 60 + 30)  # 11:30
    n_midday = midday_mask.sum()
    print(f"11:30 rows        : {n_midday:,d}  ({100*n_midday/max(len(df),1):.1f}%)")

    if not args.all_rows:
        df_audit = df[midday_mask].copy()
        print(f"\nAuditing 11:30 rows only (use --all-rows to include all intraday rows).")
    else:
        df_audit = df.copy()
        print(f"\nAuditing ALL rows (velocity columns will show high null% for non-11:30 rows).")

    if df_audit.empty:
        raise SystemExit("No 11:30 rows found. Verify dataset has been enriched (enrichment_runner.py).")

    # ── per-group audit ───────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("FEATURE GROUP AUDIT")
    print("=" * 80)

    all_ok = True
    for grp, cols in groups.items():
        is_velocity_grp = grp in VELOCITY_GROUPS
        if is_velocity_grp and not args.all_rows:
            # already filtered to 11:30 — velocity should be populated
            stats = _group_stats(df_audit, grp, cols)
        else:
            stats = _group_stats(df_audit, grp, cols)

        _print_group(stats, args.verbose)
        if stats["warn"] > 0 or stats["missing"] > 0:
            all_ok = False

    # ── overall summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    rows_per_date = df_audit.groupby("trade_date").size()
    print(f"Dates with 11:30 row : {len(rows_per_date):,d}")
    if not rows_per_date.empty:
        print(f"Rows/date min/median/max : {rows_per_date.min()} / {rows_per_date.median():.0f} / {rows_per_date.max()}")

    # Velocity coverage — what % of dates have non-null velocity
    vel_probe = "vel_price_delta_open"
    if vel_probe in df_audit.columns:
        vel_coverage = df_audit[vel_probe].notna().mean()
        print(f"Velocity coverage (vel_price_delta_open): {vel_coverage:.1%}")
        if vel_coverage < 0.8:
            print("  !! LOW — run enrichment_runner.py to backfill velocity features")

    # Daily regime coverage
    regime_probe = "regime_rv20"
    if regime_probe in df_audit.columns:
        regime_coverage = df_audit[regime_probe].notna().mean()
        print(f"Daily regime coverage (regime_rv20)     : {regime_coverage:.1%}")
        if regime_coverage < 0.8:
            print("  !! LOW — run build_daily_regime_v3.py to backfill regime features")

    print()
    if all_ok:
        print("All feature groups look HEALTHY.")
    else:
        print("Some groups have issues — see WARN/MISSING above.")


if __name__ == "__main__":
    main()
