"""Add the BMM compression/structure features to an EXISTING stage-view dataset.

Used on machines that already have a complete feature view (e.g.
stage1_entry_view_v3_candidate) but lack market_base for a full rebuild. For each
trade-date it computes the 12 compression columns from the flat OHLC (via the shared
snapshot_app.core.compression_features module — same code the live path uses, so zero
train/serve skew) and merges them onto the view day file by snapshot_id, in place.

Idempotent / resumable: a day already carrying all compression columns is skipped.

Run:
  python ml_pipeline_2/scripts/enrich_view_compression.py \
      --parquet-root ~/parquet_data \
      --view-dataset stage1_entry_view_v3_candidate \
      --flat-dataset snapshots_ml_flat_v2
"""
from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path

import pandas as pd

from snapshot_app.core.compression_features import (
    COMPRESSION_FEATURE_COLUMNS,
    add_compression_features_from_flat,
)


def _flat_day_path(flat_root: Path, trade_date: str) -> str | None:
    year = trade_date[:4]
    cand = flat_root / f"year={year}" / f"{trade_date}.parquet"
    if cand.exists():
        return str(cand)
    hits = glob.glob(str(flat_root / "**" / f"{trade_date}.parquet"), recursive=True)
    return hits[0] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet-root", required=True)
    ap.add_argument("--view-dataset", default="stage1_entry_view_v3_candidate")
    ap.add_argument("--flat-dataset", default="snapshots_ml_flat_v2")
    ap.add_argument("--force", action="store_true", help="recompute even if columns present")
    a = ap.parse_args()

    root = Path(os.path.expanduser(a.parquet_root))
    view_root = root / a.view_dataset
    flat_root = root / a.flat_dataset
    view_files = sorted(glob.glob(str(view_root / "**" / "*.parquet"), recursive=True))
    if not view_files:
        raise FileNotFoundError(f"no view parquet under {view_root}")
    print(f"view files: {len(view_files)}  flat_root: {flat_root}")

    done = skipped = no_flat = 0
    for i, vf in enumerate(view_files):
        v = pd.read_parquet(vf)
        if not a.force and all(c in v.columns for c in COMPRESSION_FEATURE_COLUMNS):
            skipped += 1
            continue
        td = str(v["trade_date"].iloc[0]) if "trade_date" in v.columns and len(v) else Path(vf).stem
        fp = _flat_day_path(flat_root, td)
        if fp is None:
            no_flat += 1
            continue
        flat = pd.read_parquet(fp).sort_values("timestamp").reset_index(drop=True)
        flat = add_compression_features_from_flat(flat)
        comp = flat[["snapshot_id", *COMPRESSION_FEATURE_COLUMNS]].copy()
        # drop any stale compression cols on the view before merge
        v = v.drop(columns=[c for c in COMPRESSION_FEATURE_COLUMNS if c in v.columns], errors="ignore")
        merged = v.merge(comp, on="snapshot_id", how="left")
        merged.to_parquet(vf, index=False, engine="pyarrow")
        done += 1
        if (done % 100) == 0:
            print(f"  enriched {done} days (last {td}, nonnull comp={int(merged[COMPRESSION_FEATURE_COLUMNS[0]].notna().sum())}/{len(merged)})", flush=True)

    print(f"done. enriched={done} skipped_already={skipped} no_flat_day={no_flat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
