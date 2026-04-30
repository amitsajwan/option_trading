# -*- coding: utf-8 -*-
"""
S2 Feature Signal Analysis - Story 2 gate.

Loads Stage 2 model from the current run, extracts feature importances
for both trade_gate and direction sub-models, then loads the Stage 2
dataset split by oracle direction label (CE vs PE) and computes per-feature
separation across validation and holdout windows.

Outputs a JSON memo to /tmp/s2_feature_analysis.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy import stats

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

RUN_DIR = Path(
    "ml_pipeline_2/artifacts/training_launches"
    "/stage3_midday_policy_paths_v1/run/runs"
    "/03_stage3_balanced_gate_fixed_guard"
)
OUTPUT_PATH = Path("/tmp/s2_feature_analysis.json")
TOP_N = 20          # top features by importance to inspect
SEP_THRESHOLD = 0.1  # minimum absolute mean difference (normalised) to call a feature "separating"
CROSS_WINDOW_THRESHOLD = 0.05  # a feature is cross-window stable if normalised diff holds in both windows


def _safe(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return None
    return round(float(v), 6)


def _extract_model_features(pkg_node, label: str) -> dict:
    """Recurse into a package dict and find the first estimator with feature_importances_."""
    if isinstance(pkg_node, dict):
        for k, v in pkg_node.items():
            result = _extract_model_features(v, f"{label}.{k}")
            if result:
                return result
    if hasattr(pkg_node, "feature_names_in_") and hasattr(pkg_node, "feature_importances_"):
        names = list(pkg_node.feature_names_in_)
        fi = pkg_node.feature_importances_
        top_idx = np.argsort(fi)[::-1][:TOP_N]
        return {
            "label": label,
            "n_features": len(names),
            "top_features": [
                {"name": names[i], "importance": round(float(fi[i]), 6)}
                for i in top_idx
            ],
            "all_feature_names": names,
            "all_importances": fi.tolist(),
        }
    return {}


def _normalised_mean_diff(ce_vals: np.ndarray, pe_vals: np.ndarray) -> float | None:
    """Cohen's d: mean difference / pooled std. Positive = CE > PE."""
    if len(ce_vals) < 5 or len(pe_vals) < 5:
        return None
    pooled_std = np.sqrt((ce_vals.std() ** 2 + pe_vals.std() ** 2) / 2.0)
    if pooled_std < 1e-10:
        return None
    return float((ce_vals.mean() - pe_vals.mean()) / pooled_std)


def _feature_separation(
    df: pd.DataFrame,
    feature_names: list[str],
    oracle_col: str = "direction_label",
) -> list[dict]:
    results = []
    ce = df[df[oracle_col].astype(str) == "CE"]
    pe = df[df[oracle_col].astype(str) == "PE"]
    for fname in feature_names:
        if fname not in df.columns:
            results.append({"feature": fname, "available": False})
            continue
        ce_vals = pd.to_numeric(ce[fname], errors="coerce").dropna().values
        pe_vals = pd.to_numeric(pe[fname], errors="coerce").dropna().values
        d = _normalised_mean_diff(ce_vals, pe_vals)
        # Mann-Whitney U for non-parametric rank separation
        mw_p = None
        if len(ce_vals) >= 5 and len(pe_vals) >= 5:
            try:
                _, mw_p = stats.mannwhitneyu(ce_vals, pe_vals, alternative="two-sided")
                mw_p = float(mw_p)
            except Exception:
                pass
        results.append({
            "feature": fname,
            "available": True,
            "ce_mean": _safe(float(ce_vals.mean()) if len(ce_vals) else None),
            "pe_mean": _safe(float(pe_vals.mean()) if len(pe_vals) else None),
            "ce_n": int(len(ce_vals)),
            "pe_n": int(len(pe_vals)),
            "cohens_d": _safe(d),
            "abs_cohens_d": _safe(abs(d) if d is not None else None),
            "mw_pvalue": _safe(mw_p),
            "significant": bool(mw_p is not None and mw_p < 0.05 and d is not None and abs(d) >= SEP_THRESHOLD),
        })
    return results


def main():
    summary = json.loads((RUN_DIR / "summary.json").read_text())
    resolved = json.loads((RUN_DIR / "resolved_config.json").read_text())

    # -----------------------------------------------------------------------
    # 1. Feature importances from both Stage 2 sub-models
    # -----------------------------------------------------------------------
    s2_path = summary["stage_artifacts"]["stage2"]["model_package_path"]
    pkg = joblib.load(s2_path)

    trade_gate_info = _extract_model_features(
        pkg.get("trade_gate_package", {}), "trade_gate"
    )
    direction_info = _extract_model_features(
        pkg.get("direction_package", {}), "direction"
    )

    print(f"trade_gate: {trade_gate_info.get('n_features')} features, "
          f"top: {[f['name'] for f in (trade_gate_info.get('top_features') or [])[:5]]}")
    print(f"direction:  {direction_info.get('n_features')} features, "
          f"top: {[f['name'] for f in (direction_info.get('top_features') or [])[:5]]}")

    # -----------------------------------------------------------------------
    # 2. Load Stage 2 dataset with oracle labels
    # -----------------------------------------------------------------------
    sys.path.insert(0, str(Path("ml_pipeline_2/src").resolve()))
    from ml_pipeline_2.staged.pipeline import (
        _apply_runtime_filters,
        _build_oracle_targets,
        _load_dataset,
        _merge_policy_inputs,
        _window,
    )
    from ml_pipeline_2.staged.counterfactual import _resolve_recipe_universe
    from ml_pipeline_2.staged.registries import view_registry

    parquet_root = Path(resolved["inputs"]["parquet_root"])
    support_dataset = resolved["inputs"]["support_dataset"]
    runtime_block_expiry = bool(resolved.get("runtime", {}).get("block_expiry", False))
    cost_per_trade = float((resolved.get("training") or {}).get("cost_per_trade", 0.0))

    support_raw = _load_dataset(parquet_root, support_dataset)
    support_context = support_raw.loc[:, ~support_raw.columns.duplicated()].copy()
    support_filtered, _ = _apply_runtime_filters(
        support_raw, block_expiry=runtime_block_expiry,
        context="s2 feature analysis support"
    )

    recipe_universe = _resolve_recipe_universe(
        run_recipe_catalog_id=str(summary.get("recipe_catalog_id") or ""),
        fixed_recipe_ids=["L3", "L6"],
    )
    oracle, _ = _build_oracle_targets(support_filtered, recipe_universe, cost_per_trade=cost_per_trade)

    # Stage 2 dataset
    s2_view_id = summary["component_ids"]["stage2"]["view_id"]
    s2_dataset_name = view_registry()[s2_view_id].dataset_name
    s2_raw = _load_dataset(parquet_root, s2_dataset_name)
    s2_filtered, _ = _apply_runtime_filters(
        s2_raw, block_expiry=runtime_block_expiry,
        support_context=support_context, context="s2 feature analysis stage2"
    )

    # Merge oracle labels onto Stage 2 rows
    KEY = ["trade_date", "timestamp", "snapshot_id"]
    s2_with_labels = _merge_policy_inputs(s2_filtered, oracle[KEY + ["entry_label", "direction_label"]])
    # Keep only oracle-positive rows (entry_label == 1) for direction analysis
    oracle_positive = s2_with_labels[
        pd.to_numeric(s2_with_labels["entry_label"], errors="coerce").fillna(0).astype(int) == 1
    ].copy()
    print(f"oracle_positive rows: {len(oracle_positive)}")
    print(f"direction_label dist: {oracle_positive['direction_label'].value_counts().to_dict()}")

    # -----------------------------------------------------------------------
    # 3. Window splits
    # -----------------------------------------------------------------------
    valid_cfg = resolved["windows"]["research_valid"]
    holdout_cfg = resolved["windows"]["final_holdout"]
    valid_pos = _window(oracle_positive, valid_cfg)
    holdout_pos = _window(oracle_positive, holdout_cfg)
    print(f"valid oracle_positive: {len(valid_pos)} "
          f"(CE={( valid_pos['direction_label']=='CE').sum()}, "
          f"PE={(valid_pos['direction_label']=='PE').sum()})")
    print(f"holdout oracle_positive: {len(holdout_pos)} "
          f"(CE={(holdout_pos['direction_label']=='CE').sum()}, "
          f"PE={(holdout_pos['direction_label']=='PE').sum()})")

    # -----------------------------------------------------------------------
    # 4. Feature separation analysis — direction model features
    # -----------------------------------------------------------------------
    direction_feature_names = direction_info.get("all_feature_names", [])
    top_direction_features = [f["name"] for f in (direction_info.get("top_features") or [])]

    sep_valid = _feature_separation(valid_pos, top_direction_features)
    sep_holdout = _feature_separation(holdout_pos, top_direction_features)
    sep_combined = _feature_separation(oracle_positive, top_direction_features)

    # Cross-window stability: feature is stable if it is significant AND
    # the sign of cohens_d agrees between validation and holdout
    cross_window_stable = []
    for sv, sh in zip(sep_valid, sep_holdout):
        fname = sv["feature"]
        d_v = sv.get("cohens_d")
        d_h = sh.get("cohens_d")
        sig_v = sv.get("significant", False)
        sig_h = sh.get("significant", False)
        same_sign = (d_v is not None and d_h is not None and
                     np.sign(d_v) == np.sign(d_h) and
                     abs(d_v) >= SEP_THRESHOLD and abs(d_h) >= SEP_THRESHOLD)
        cross_window_stable.append({
            "feature": fname,
            "cohens_d_valid": d_v,
            "cohens_d_holdout": d_h,
            "significant_valid": sig_v,
            "significant_holdout": sig_h,
            "cross_window_stable": bool(same_sign),
        })

    n_stable = sum(1 for r in cross_window_stable if r["cross_window_stable"])
    signal_exists = n_stable >= 3  # at least 3 top features stable across windows

    # -----------------------------------------------------------------------
    # 5. Regime drift check: are top features themselves different across windows?
    # -----------------------------------------------------------------------
    regime_drift = []
    for fname in top_direction_features[:10]:
        if fname not in oracle_positive.columns:
            continue
        v_vals = pd.to_numeric(valid_pos[fname], errors="coerce").dropna().values
        h_vals = pd.to_numeric(holdout_pos[fname], errors="coerce").dropna().values
        d = _normalised_mean_diff(v_vals, h_vals)
        regime_drift.append({
            "feature": fname,
            "valid_mean": _safe(float(v_vals.mean()) if len(v_vals) else None),
            "holdout_mean": _safe(float(h_vals.mean()) if len(h_vals) else None),
            "cohens_d_valid_vs_holdout": _safe(d),
            "feature_drifts_across_regime": bool(d is not None and abs(d) >= 0.20),
        })

    # -----------------------------------------------------------------------
    # 6. Write memo
    # -----------------------------------------------------------------------
    memo = {
        "story": "S2 - Stage 2 feature signal analysis",
        "run_dir": str(RUN_DIR),
        "oracle_positive_total": int(len(oracle_positive)),
        "oracle_positive_valid": int(len(valid_pos)),
        "oracle_positive_holdout": int(len(holdout_pos)),
        "direction_model_n_features": direction_info.get("n_features"),
        "trade_gate_model_n_features": trade_gate_info.get("n_features"),
        "top_direction_importances": direction_info.get("top_features", [])[:TOP_N],
        "top_trade_gate_importances": trade_gate_info.get("top_features", [])[:10],
        "separation_valid": sep_valid,
        "separation_holdout": sep_holdout,
        "separation_combined": sep_combined,
        "cross_window_stability": cross_window_stable,
        "n_cross_window_stable_features": int(n_stable),
        "regime_feature_drift": regime_drift,
        "signal_exists": bool(signal_exists),
        "verdict": (
            "YES - at least 3 top direction features show cross-window stable CE/PE separation. "
            "Retrain with regime-state context is justified."
            if signal_exists else
            "NO - fewer than 3 top direction features maintain CE/PE separation across both windows. "
            "Current feature set cannot support regime-robust direction prediction. Stop or redesign features."
        ),
    }

    OUTPUT_PATH.write_text(json.dumps(memo, indent=2, default=str), encoding="utf-8")
    print()
    print("=" * 60)
    print(f"SIGNAL EXISTS: {signal_exists}")
    print(f"Cross-window stable features: {n_stable} / {len(top_direction_features)}")
    print(f"VERDICT: {memo['verdict']}")
    print(f"Full memo: {OUTPUT_PATH}")

    # Print top stable features
    stable = [r for r in cross_window_stable if r["cross_window_stable"]]
    if stable:
        print("\nStable CE/PE separating features:")
        for r in stable:
            print(f"  {r['feature']}: d_valid={r['cohens_d_valid']:.3f}, d_holdout={r['cohens_d_holdout']:.3f}")
    else:
        print("\nNo cross-window stable separating features found.")


if __name__ == "__main__":
    main()
