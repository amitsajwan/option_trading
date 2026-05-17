"""Production driver — emit option-P&L labels for a date range.

Reads snapshots_ml_flat_v2 + options parquet, runs the labeler per
(snapshot row × recipe), writes per-date label parquet partitions.

Per-date partition layout:
    {out_root}/labels/year=YYYY/{YYYY-MM-DD}.parquet
    {out_root}/skipped/year=YYYY/{YYYY-MM-DD}.parquet     (debug only)
    {out_root}/_runs/{YYYY-MM-DD}.json                    (per-date manifest)

Idempotency: if {YYYY-MM-DD}.parquet already exists, skip processing
unless --overwrite. This makes the multi-year run resumable.

Usage:
    python -m ml_pipeline_2.scripts.build_option_pnl_labels \\
      --date-from 2020-04-01 --date-to 2024-12-31 \\
      --out /opt/option_trading/.data/ml_pipeline/parquet_data/option_pnl_labels_v1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Optional

import pandas as pd

from ml_pipeline_2.labeling.option_pnl import (
    LabelContract,
    Recipe,
    SKIP_LABEL,
    label_one,
)
from ml_pipeline_2.scripts.option_pnl_smoke import (
    DEFAULT_FLAT_ROOT,
    DEFAULT_OPTIONS_ROOT,
    ParquetDayLookup,
    derive_strike_step,
    load_options_for_month,
    load_snapshots_for_date,
    make_recipes_v1,
    pick_expiry_for_date,
)


def process_date(
    *,
    trade_date: pd.Timestamp,
    flat_root: Path,
    options_root: Path,
    out_root: Path,
    recipes: list[Recipe],
    contract: LabelContract,
    overwrite: bool,
) -> dict:
    """Process one trade date end-to-end. Returns a manifest dict."""
    date_str = trade_date.strftime("%Y-%m-%d")
    year = trade_date.year
    labels_path = out_root / "labels" / f"year={year}" / f"{date_str}.parquet"
    skipped_path = out_root / "skipped" / f"year={year}" / f"{date_str}.parquet"
    manifest_path = out_root / "_runs" / f"{date_str}.json"

    manifest = {
        "date": date_str,
        "started_at": pd.Timestamp.utcnow().isoformat(),
        "labels_path": str(labels_path),
        "skipped_path": str(skipped_path),
    }

    if labels_path.exists() and not overwrite:
        manifest["status"] = "skipped_exists"
        return manifest

    try:
        snaps = load_snapshots_for_date(flat_root, trade_date)
    except FileNotFoundError:
        manifest["status"] = "no_snapshot_parquet"
        return manifest

    try:
        options = load_options_for_month(options_root, trade_date)
    except FileNotFoundError:
        manifest["status"] = "no_options_parquet"
        return manifest

    if options.empty:
        manifest["status"] = "empty_options_for_date"
        return manifest

    expiry_str = pick_expiry_for_date(options, trade_date)
    strike_step = derive_strike_step(options)
    if expiry_str is None or strike_step is None:
        manifest["status"] = "missing_expiry_or_step"
        return manifest

    manifest["expiry_str"] = expiry_str
    manifest["strike_step"] = int(strike_step)
    manifest["snapshot_rows"] = int(len(snaps))

    lookup = ParquetDayLookup(options, expiry_str)

    label_rows: list[dict] = []
    skipped_rows: list[dict] = []
    per_recipe = {r.id: Counter() for r in recipes}

    for _, srow in snaps.iterrows():
        atm = srow.get("opt_flow_atm_strike")
        if pd.isna(atm):
            for r in recipes:
                per_recipe[r.id]["snapshot_missing_atm"] += 1
            continue
        snap_min = int(srow["minute"])
        snap_dict = {
            "timestamp_minute": snap_min,
            "trade_date": date_str,
            "atm_strike": int(atm),
            "strike_step": strike_step,
            "expiry_str": expiry_str,
        }
        snap_id = str(srow.get("snapshot_id") or f"{date_str}_{snap_min:04d}")

        for r in recipes:
            out = label_one(snapshot=snap_dict, recipe=r, lookup=lookup, contract=contract)
            base = {
                "trade_date": date_str,
                "snapshot_id": snap_id,
                "timestamp_minute": snap_min,
                "recipe_id": r.id,
                "atm_strike": int(atm),
            }
            if out["label"] == SKIP_LABEL:
                per_recipe[r.id][out["reason_skipped"]] += 1
                skipped_rows.append({**base, "reason_skipped": out["reason_skipped"]})
            else:
                per_recipe[r.id][f"label_{out['label']}"] += 1
                label_rows.append({**base, **{k: v for k, v in out.items() if k != "reason_skipped"}})

    # Write outputs
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    skipped_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    if label_rows:
        pd.DataFrame(label_rows).to_parquet(labels_path, index=False)
    else:
        # Write empty file so resume logic doesn't reprocess
        pd.DataFrame(columns=["trade_date", "snapshot_id", "recipe_id", "label"]).to_parquet(labels_path, index=False)

    if skipped_rows:
        pd.DataFrame(skipped_rows).to_parquet(skipped_path, index=False)

    manifest.update({
        "status": "ok",
        "labels_emitted": len(label_rows),
        "skipped_count": len(skipped_rows),
        "per_recipe_counts": {rid: dict(c) for rid, c in per_recipe.items()},
        "finished_at": pd.Timestamp.utcnow().isoformat(),
    })
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def daterange(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """Inclusive list of business days (Mon-Fri) — skips weekends. Holiday
    filtering is implicit: if no snapshot parquet exists for a date, the
    driver records 'no_snapshot_parquet' and moves on."""
    return [d for d in pd.date_range(start=start, end=end, freq="B")]


def run(args) -> int:
    flat_root = Path(args.flat_root)
    options_root = Path(args.options_root)
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    start = pd.Timestamp(args.date_from)
    end = pd.Timestamp(args.date_to)
    if end < start:
        print("ERROR: --date-to before --date-from", file=sys.stderr)
        return 2

    dates = daterange(start, end)
    print(f"=== Option-P&L labels driver ===")
    print(f"date range: {start.date()} → {end.date()}  ({len(dates)} business days)")
    print(f"out: {out_root}")
    print(f"overwrite: {args.overwrite}")

    recipes = make_recipes_v1()
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
                out_root=out_root, recipes=recipes, contract=contract, overwrite=args.overwrite,
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
        except Exception as exc:  # noqa: BLE001 — driver must not die on one bad date
            status_counts["uncaught_exception"] += 1
            print(f"  [{i+1}/{len(dates)}] {d.date()} EXCEPTION: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            (out_root / "_runs" / f"{d.strftime('%Y-%m-%d')}.error.json").write_text(
                json.dumps({"date": str(d.date()), "error": str(exc), "traceback": traceback.format_exc()}, indent=2)
            )

    print()
    print(f"=== Driver complete ===")
    print(f"total labels emitted: {total_labels:,}")
    print(f"total skipped rows:   {total_skipped:,}")
    print(f"status breakdown:")
    for s, c in status_counts.most_common():
        print(f"  {s:<30s} {c}")
    if status_counts.get("ok", 0) == 0:
        print("FAIL: no days processed OK", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Build option-P&L labels parquet over a date range")
    p.add_argument("--date-from", required=True)
    p.add_argument("--date-to", required=True)
    p.add_argument("--out", required=True, help="Output root (creates labels/, skipped/, _runs/)")
    p.add_argument("--flat-root", default=str(DEFAULT_FLAT_ROOT))
    p.add_argument("--options-root", default=str(DEFAULT_OPTIONS_ROOT))
    p.add_argument("--overwrite", action="store_true", help="Re-process dates whose labels parquet already exists")
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
