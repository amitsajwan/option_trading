# -*- coding: utf-8 -*-
"""Stage 2 feature signal analysis — Story 2 gate script."""
from __future__ import annotations
import joblib, json, numpy as np, pandas as pd, sys, glob
from pathlib import Path
from scipy import stats

BASE = Path("ml_pipeline_2/artifacts/training_launches"
            "/stage3_midday_policy_paths_v1/run/runs"
            "/03_stage3_balanced_gate_fixed_guard")

summary = json.loads((BASE / "summary.json").read_text())
resolved = json.loads((BASE / "resolved_config.json").read_text())

pkg = joblib.load(summary["stage_artifacts"]["stage2"]["model_package_path"])
dir_features = pkg["direction_package"]["feature_columns"]

parquet_root = Path(resolved["inputs"]["parquet_root"])
windows = resolved["windows"]
valid_start  = windows["research_valid"]["start"]
valid_end    = windows["research_valid"]["end"]
holdout_start = windows["final_holdout"]["start"]
holdout_end   = windows["final_holdout"]["end"]

s2_files = sorted(glob.glob(
    str(parquet_root / "stage2_direction_view" / "**" / "*.parquet"), recursive=True))
sup_files = sorted(glob.glob(
    str(parquet_root / resolved["inputs"]["support_dataset"] / "**" / "*.parquet"), recursive=True))
print(f"s2 files: {len(s2_files)}, support files: {len(sup_files)}")

s2_df  = pd.concat([pd.read_parquet(f) for f in s2_files],  ignore_index=True)
sup_df = pd.concat([pd.read_parquet(f) for f in sup_files], ignore_index=True)
print(f"Stage2: {len(s2_df)} rows   Support: {len(sup_df)} rows")

sys.path.insert(0, "ml_pipeline_2/src")
from ml_pipeline_2.staged.pipeline import _build_oracle_targets
from ml_pipeline_2.staged.counterfactual import _resolve_recipe_universe

recipe_universe = _resolve_recipe_universe(
    run_recipe_catalog_id=str(summary.get("recipe_catalog_id") or ""),
    fixed_recipe_ids=["L3", "L6"],
)
cost = float((resolved.get("training") or {}).get("cost_per_trade", 0.0))
oracle, _ = _build_oracle_targets(sup_df, recipe_universe, cost_per_trade=cost)
print(f"Oracle: {len(oracle)} rows   entry positives: {oracle.entry_label.sum()}")

KEY = ["trade_date", "timestamp", "snapshot_id"]
merged = s2_df.merge(oracle[KEY + ["entry_label", "direction_label"]], on=KEY, how="inner")
oracle_pos = merged[
    pd.to_numeric(merged["entry_label"], errors="coerce").fillna(0).astype(int) == 1
].copy()
print(f"Oracle-positive: {len(oracle_pos)}")

oracle_pos["trade_date"] = pd.to_datetime(oracle_pos["trade_date"])
valid_pos   = oracle_pos[(oracle_pos["trade_date"] >= valid_start)   & (oracle_pos["trade_date"] <= valid_end)]
holdout_pos = oracle_pos[(oracle_pos["trade_date"] >= holdout_start) & (oracle_pos["trade_date"] <= holdout_end)]
print(f"Valid  : {len(valid_pos):5d}   CE={(valid_pos.direction_label=='CE').sum():4d}  PE={(valid_pos.direction_label=='PE').sum():4d}")
print(f"Holdout: {len(holdout_pos):5d}   CE={(holdout_pos.direction_label=='CE').sum():4d}  PE={(holdout_pos.direction_label=='PE').sum():4d}")


def sep(df, fname):
    if fname not in df.columns:
        return None, None
    ce = pd.to_numeric(df.loc[df.direction_label == "CE", fname], errors="coerce").dropna().values
    pe = pd.to_numeric(df.loc[df.direction_label == "PE", fname], errors="coerce").dropna().values
    if len(ce) < 10 or len(pe) < 10:
        return None, None
    pool_std = np.sqrt((ce.std() ** 2 + pe.std() ** 2) / 2)
    d = float((ce.mean() - pe.mean()) / pool_std) if pool_std > 1e-9 else 0.0
    _, p = stats.mannwhitneyu(ce, pe, alternative="two-sided")
    return d, float(p)


print()
print(f"{'feature':<28} {'d_valid':>8} {'d_holdout':>10} {'p_v':>7} {'p_h':>7} {'stable':>7} {'pattern':>10}")
print("-" * 80)

stable_count = 0
stable_features = []
results = []
for fname in dir_features:
    dv, pv = sep(valid_pos, fname)
    dh, ph = sep(holdout_pos, fname)
    stable = (
        dv is not None and dh is not None
        and abs(dv) >= 0.10 and abs(dh) >= 0.10
        and np.sign(dv) == np.sign(dh)
    )
    if stable:
        stable_count += 1
        stable_features.append(fname)
    pat = ""
    if dv is not None and dh is not None:
        pat = "CE>PE" if (dv > 0 and dh > 0) else ("PE>CE" if (dv < 0 and dh < 0) else "FLIPS")
    results.append(dict(feature=fname, d_valid=dv, d_holdout=dh, p_valid=pv, p_holdout=ph,
                        stable=stable, pattern=pat))
    print(f"{fname:<28} {str(round(dv,3)) if dv is not None else 'None':>8} "
          f"{str(round(dh,3)) if dh is not None else 'None':>10} "
          f"{str(round(pv,4)) if pv is not None else 'N/A':>7} "
          f"{str(round(ph,4)) if ph is not None else 'N/A':>7} "
          f"{'YES' if stable else 'no':>7} {pat:>10}")

print()
print(f"Cross-window stable features: {stable_count}/{len(dir_features)}")
signal_exists = stable_count >= 3
if signal_exists:
    print(f"VERDICT: YES - {stable_count} features show cross-window CE/PE separation")
    print(f"Stable features: {stable_features}")
else:
    print(f"VERDICT: NO - only {stable_count} cross-window stable features (need >=3)")

print()
print("=== REGIME DRIFT (feature mean shift valid -> holdout, unconditional) ===")
print(f"{'feature':<28} {'mean_valid':>12} {'mean_holdout':>14} {'drift_d':>9}")
print("-" * 68)
for fname in dir_features:
    if fname not in oracle_pos.columns:
        continue
    vv = pd.to_numeric(valid_pos[fname],   errors="coerce").dropna().values
    hv = pd.to_numeric(holdout_pos[fname], errors="coerce").dropna().values
    if len(vv) < 5 or len(hv) < 5:
        continue
    pool = np.sqrt((vv.std() ** 2 + hv.std() ** 2) / 2)
    d = float((vv.mean() - hv.mean()) / pool) if pool > 1e-9 else 0.0
    print(f"{fname:<28} {vv.mean():>+12.4f} {hv.mean():>+14.4f} {d:>+9.3f}")

print()
print("=== SUMMARY FOR S2 MEMO ===")
print(f"Direction model: LogisticRegression, {len(dir_features)} features")
print(f"Oracle-positive rows: valid={len(valid_pos)}, holdout={len(holdout_pos)}")
print(f"Cross-window stable CE/PE separating features: {stable_count}/{len(dir_features)}")
print(f"Signal exists (>=3 stable): {signal_exists}")
print()
if signal_exists:
    print("ACTION: Retrain is justified. Proceed to Story 3 with these stable features as anchors.")
else:
    print("ACTION: Retrain is NOT justified with current feature set.")
    print("        Current features lack cross-window directional signal.")
    print("        Options: add regime-state features (S3-A) or stop the wedge (S5).")
