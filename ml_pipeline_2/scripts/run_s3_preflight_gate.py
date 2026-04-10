# -*- coding: utf-8 -*-
"""
S3 Pre-flight Gate — Rolling Oracle Feature CE/PE Separation Check.

Runs BEFORE the full S3 GCP training grid to verify that the new rolling
oracle win-rate features (computed by compute_rolling_oracle_stats) show
cross-window stable CE/PE separation at Cohen's d >= 0.10.

Pass condition: >= 2 features stable (lower bar than S2 gate because we only
have 6 rolling oracle features vs 24 in the full direction model).

Usage (from repo root):
    python -m ml_pipeline_2.scripts.run_s3_preflight_gate
  or:
    python ml_pipeline_2/scripts/run_s3_preflight_gate.py

Writes verdict to stdout and exits 0 (pass) or 1 (fail).
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

# ---------------------------------------------------------------------------
# Config — points to the EXISTING run that has oracle and support data paths
# ---------------------------------------------------------------------------
RUN_DIR = Path(
    "ml_pipeline_2/artifacts/training_launches"
    "/stage3_midday_policy_paths_v1/run/runs"
    "/03_stage3_balanced_gate_fixed_guard"
)
MIN_STABLE = 2          # pass threshold for rolling oracle features alone
COHENS_D_MIN = 0.10


def _cohens_d(ce: np.ndarray, pe: np.ndarray) -> tuple[float | None, float | None]:
    if len(ce) < 10 or len(pe) < 10:
        return None, None
    pool_std = np.sqrt((ce.std() ** 2 + pe.std() ** 2) / 2.0)
    if pool_std < 1e-10:
        return None, None
    d = float((ce.mean() - pe.mean()) / pool_std)
    _, p = stats.mannwhitneyu(ce, pe, alternative="two-sided")
    return d, float(p)


def main() -> int:
    resolved = json.loads((RUN_DIR / "resolved_config.json").read_text())
    parquet_root = Path(resolved["inputs"]["parquet_root"])
    support_dataset = resolved["inputs"]["support_dataset"]
    windows_cfg = resolved["windows"]

    # Load support dataset
    sup_files = sorted(glob.glob(
        str(parquet_root / support_dataset / "**" / "*.parquet"), recursive=True))
    if not sup_files:
        print(f"ERROR: no parquet files found under {parquet_root / support_dataset}")
        return 1
    print(f"Loading {len(sup_files)} support parquet files...")
    sup_df = pd.concat([pd.read_parquet(f) for f in sup_files], ignore_index=True)
    print(f"Support rows: {len(sup_df)}")

    # Build oracle using pipeline
    sys.path.insert(0, "ml_pipeline_2/src")
    from ml_pipeline_2.staged.pipeline import (
        _build_oracle_targets,
        compute_rolling_oracle_stats,
    )
    from ml_pipeline_2.staged.counterfactual import _resolve_recipe_universe

    recipe_universe = _resolve_recipe_universe(
        run_recipe_catalog_id=str(resolved.get("catalog", {}).get("recipe_catalog_id") or ""),
        fixed_recipe_ids=["L3", "L6"],
    )
    cost = float((resolved.get("training") or {}).get("cost_per_trade", 0.0))
    oracle, _ = _build_oracle_targets(sup_df, recipe_universe, cost_per_trade=cost)
    print(f"Oracle: {len(oracle)} rows, entry positives: {oracle['entry_label'].sum()}")

    # Compute rolling oracle features
    rolling = compute_rolling_oracle_stats(oracle, windows=(5, 10))
    print(f"Rolling stats: {len(rolling)} trade-dates, columns: {[c for c in rolling.columns if c != 'trade_date']}")

    # Merge rolling stats with oracle so we can split CE/PE by direction_label
    oracle["trade_date"] = pd.to_datetime(oracle["trade_date"])
    rolling["trade_date"] = pd.to_datetime(rolling["trade_date"])
    merged = oracle[oracle["entry_label"].astype(float) == 1.0].merge(rolling, on="trade_date", how="left")

    # Window splits
    valid_start = pd.Timestamp(windows_cfg["research_valid"]["start"])
    valid_end = pd.Timestamp(windows_cfg["research_valid"]["end"])
    holdout_start = pd.Timestamp(windows_cfg["final_holdout"]["start"])
    holdout_end = pd.Timestamp(windows_cfg["final_holdout"]["end"])

    valid_pos = merged[(merged["trade_date"] >= valid_start) & (merged["trade_date"] <= valid_end)]
    holdout_pos = merged[(merged["trade_date"] >= holdout_start) & (merged["trade_date"] <= holdout_end)]
    print(f"\nValid  oracle+ : {len(valid_pos):5d}  CE={(valid_pos.direction_label=='CE').sum():4d}  PE={(valid_pos.direction_label=='PE').sum():4d}")
    print(f"Holdout oracle+: {len(holdout_pos):5d}  CE={(holdout_pos.direction_label=='CE').sum():4d}  PE={(holdout_pos.direction_label=='PE').sum():4d}")

    rolling_feature_cols = [c for c in rolling.columns if c != "trade_date"]
    print(f"\n{'feature':<36} {'d_valid':>8} {'d_holdout':>10} {'p_v':>7} {'p_h':>7} {'stable':>7} {'pattern':>8}")
    print("-" * 84)

    stable_count = 0
    stable_features: list[str] = []
    for fname in rolling_feature_cols:
        for split_df, label in [(valid_pos, "valid"), (holdout_pos, "holdout")]:
            if fname not in split_df.columns:
                break
        else:
            ce_v = pd.to_numeric(valid_pos.loc[valid_pos.direction_label == "CE", fname], errors="coerce").dropna().values
            pe_v = pd.to_numeric(valid_pos.loc[valid_pos.direction_label == "PE", fname], errors="coerce").dropna().values
            ce_h = pd.to_numeric(holdout_pos.loc[holdout_pos.direction_label == "CE", fname], errors="coerce").dropna().values
            pe_h = pd.to_numeric(holdout_pos.loc[holdout_pos.direction_label == "PE", fname], errors="coerce").dropna().values
            dv, pv = _cohens_d(ce_v, pe_v)
            dh, ph = _cohens_d(ce_h, pe_h)
            stable = (
                dv is not None and dh is not None
                and abs(dv) >= COHENS_D_MIN and abs(dh) >= COHENS_D_MIN
                and np.sign(dv) == np.sign(dh)
            )
            if stable:
                stable_count += 1
                stable_features.append(fname)
            pat = ""
            if dv is not None and dh is not None:
                pat = "CE>PE" if (dv > 0 and dh > 0) else ("PE>CE" if (dv < 0 and dh < 0) else "FLIPS")
            print(
                f"{fname:<36} {str(round(dv, 3)) if dv is not None else 'None':>8} "
                f"{str(round(dh, 3)) if dh is not None else 'None':>10} "
                f"{str(round(pv, 4)) if pv is not None else 'N/A':>7} "
                f"{str(round(ph, 4)) if ph is not None else 'N/A':>7} "
                f"{'YES' if stable else 'no':>7} {pat:>8}"
            )

    print()
    print(f"Stable rolling features: {stable_count}/{len(rolling_feature_cols)}  (need >= {MIN_STABLE})")

    if stable_count >= MIN_STABLE:
        print(f"\nPREFLIGHT PASS — rolling oracle features show cross-window CE/PE signal.")
        print(f"Stable: {stable_features}")
        print("Next step: run the full S3 grid on GCP.")
        print("  python -m ml_pipeline_2.run_staged_grid ml_pipeline_2/configs/research/staged_grid.stage3_direction_regime_v1.json")
        return 0
    else:
        print(f"\nPREFLIGHT FAIL — only {stable_count} stable features (need {MIN_STABLE}).")
        print("Rolling oracle features do not show CE/PE separation.")
        print("Before running GCP, investigate:")
        print("  1. Are CE/PE oracle counts balanced across windows?")
        print("  2. Is the rolling window long enough to capture regime shifts?")
        print("  3. Consider adding trend-consistency or VIX-regime features.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
