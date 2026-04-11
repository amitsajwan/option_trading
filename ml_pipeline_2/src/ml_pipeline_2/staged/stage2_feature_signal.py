from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

import numpy as np
import pandas as pd
from scipy import stats

from ..experiment_control.state import utc_now
from .stage2_diagnostic_common import build_stage2_scored_window_frame, load_stage2_diagnostic_context

DEFAULT_STAGE2_FEATURE_SIGNAL_FIXED_RECIPE_IDS: tuple[str, ...] = ("L3", "L6")


def _safe_float(value: object) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if np.isfinite(out) else None


def _final_estimator(model: object) -> object:
    if hasattr(model, "steps"):
        steps = list(getattr(model, "steps") or [])
        if steps:
            return steps[-1][1]
    return model


def _extract_weighted_features(model_package: Dict[str, Any], *, top_k: int = 5) -> dict[str, Any]:
    direction_package = dict(model_package.get("direction_package") or {})
    feature_columns = [str(col) for col in list(direction_package.get("feature_columns") or [])]
    model = ((direction_package.get("models") or {}).get("direction"))
    estimator = _final_estimator(model)
    weights: list[float] = []
    weight_kind = "unknown"
    if hasattr(estimator, "coef_"):
        coef = np.asarray(getattr(estimator, "coef_"))
        if coef.ndim == 2 and coef.shape[0] > 0:
            weights = coef[0].astype(float).tolist()
            weight_kind = "coefficient"
    elif hasattr(estimator, "feature_importances_"):
        weights = np.asarray(getattr(estimator, "feature_importances_")).astype(float).tolist()
        weight_kind = "importance"

    pairs = list(zip(feature_columns, weights)) if weights and len(weights) == len(feature_columns) else []
    top_weighted_features = [
        {
            "feature": feature,
            "weight": round(float(weight), 6),
            "abs_weight": round(abs(float(weight)), 6),
        }
        for feature, weight in sorted(pairs, key=lambda item: abs(float(item[1])), reverse=True)[: int(top_k)]
    ]
    return {
        "model_family": type(estimator).__name__,
        "feature_columns": feature_columns,
        "weight_kind": weight_kind,
        "top_weighted_features": top_weighted_features,
    }


def _direction_oracle_frame(
    merged: pd.DataFrame,
    *,
    stage1_positive_only: bool,
    entry_threshold: float | None,
) -> pd.DataFrame:
    out = merged.copy()
    if "entry_label" not in out.columns or "direction_label" not in out.columns:
        return out.iloc[0:0].copy()
    entry_mask = pd.to_numeric(out["entry_label"], errors="coerce").fillna(0).astype(int).eq(1)
    direction_mask = out["direction_label"].astype(str).isin(["CE", "PE"])
    out = out.loc[entry_mask & direction_mask].copy()
    if stage1_positive_only and entry_threshold is not None:
        entry_prob = pd.to_numeric(out["entry_prob"], errors="coerce").fillna(0.0)
        out = out.loc[entry_prob >= float(entry_threshold)].copy()
    return out.reset_index(drop=True)


def _cohens_d(ce_values: np.ndarray, pe_values: np.ndarray) -> float | None:
    if len(ce_values) < 5 or len(pe_values) < 5:
        return None
    ce_mean = float(np.mean(ce_values))
    pe_mean = float(np.mean(pe_values))
    mean_diff = ce_mean - pe_mean
    pooled_std = np.sqrt((np.std(ce_values) ** 2 + np.std(pe_values) ** 2) / 2.0)
    if pooled_std <= 1e-12:
        if abs(mean_diff) <= 1e-12:
            return 0.0
        # Perfect separation with zero within-class variance should still count as signal.
        return float(np.sign(mean_diff) * 999.0)
    return float(mean_diff / float(pooled_std))


def _feature_separation(frame: pd.DataFrame, feature_columns: Sequence[str]) -> list[dict[str, Any]]:
    ce = frame.loc[frame["direction_label"].astype(str).eq("CE")]
    pe = frame.loc[frame["direction_label"].astype(str).eq("PE")]
    rows: list[dict[str, Any]] = []
    for feature in feature_columns:
        if feature not in frame.columns:
            rows.append({"feature": str(feature), "available": False})
            continue
        ce_values = pd.to_numeric(ce[feature], errors="coerce").dropna().to_numpy()
        pe_values = pd.to_numeric(pe[feature], errors="coerce").dropna().to_numpy()
        effect = _cohens_d(ce_values, pe_values)
        p_value = None
        if len(ce_values) >= 5 and len(pe_values) >= 5:
            try:
                _, p_value = stats.mannwhitneyu(ce_values, pe_values, alternative="two-sided")
                p_value = float(p_value)
            except Exception:
                p_value = None
        rows.append(
            {
                "feature": str(feature),
                "available": True,
                "ce_n": int(len(ce_values)),
                "pe_n": int(len(pe_values)),
                "ce_mean": _safe_float(np.mean(ce_values)) if len(ce_values) else None,
                "pe_mean": _safe_float(np.mean(pe_values)) if len(pe_values) else None,
                "cohens_d": _safe_float(effect),
                "abs_cohens_d": _safe_float(abs(float(effect))) if effect is not None else None,
                "mann_whitney_pvalue": _safe_float(p_value),
            }
        )
    return rows


def _cross_window_stability(
    validation_rows: Sequence[Dict[str, Any]],
    holdout_rows: Sequence[Dict[str, Any]],
    *,
    min_effect_size: float,
    max_p_value: float,
) -> list[dict[str, Any]]:
    holdout_by_feature = {str(row.get("feature")): dict(row) for row in holdout_rows}
    out: list[dict[str, Any]] = []
    for validation_row in validation_rows:
        feature = str(validation_row.get("feature"))
        holdout_row = holdout_by_feature.get(feature, {})
        validation_d = _safe_float(validation_row.get("cohens_d"))
        holdout_d = _safe_float(holdout_row.get("cohens_d"))
        validation_p = _safe_float(validation_row.get("mann_whitney_pvalue"))
        holdout_p = _safe_float(holdout_row.get("mann_whitney_pvalue"))
        stable = bool(
            validation_d is not None
            and holdout_d is not None
            and validation_p is not None
            and holdout_p is not None
            and abs(validation_d) >= float(min_effect_size)
            and abs(holdout_d) >= float(min_effect_size)
            and validation_p <= float(max_p_value)
            and holdout_p <= float(max_p_value)
            and np.sign(validation_d) == np.sign(holdout_d)
        )
        out.append(
            {
                "feature": feature,
                "cohens_d_validation": validation_d,
                "cohens_d_holdout": holdout_d,
                "p_value_validation": validation_p,
                "p_value_holdout": holdout_p,
                "same_sign": bool(
                    validation_d is not None and holdout_d is not None and np.sign(validation_d) == np.sign(holdout_d)
                ),
                "cross_window_stable": stable,
            }
        )
    return out


def _regime_drift(validation_frame: pd.DataFrame, holdout_frame: pd.DataFrame, feature_columns: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in feature_columns:
        if feature not in validation_frame.columns or feature not in holdout_frame.columns:
            continue
        validation_values = pd.to_numeric(validation_frame[feature], errors="coerce").dropna().to_numpy()
        holdout_values = pd.to_numeric(holdout_frame[feature], errors="coerce").dropna().to_numpy()
        rows.append(
            {
                "feature": str(feature),
                "validation_mean": _safe_float(np.mean(validation_values)) if len(validation_values) else None,
                "holdout_mean": _safe_float(np.mean(holdout_values)) if len(holdout_values) else None,
                "cohens_d_validation_vs_holdout": _safe_float(_cohens_d(validation_values, holdout_values)),
            }
        )
    return rows


def _feature_table_markdown(rows: Sequence[Dict[str, Any]], *, stable_only: bool = False) -> str:
    filtered = list(rows)
    if stable_only:
        filtered = [row for row in filtered if bool(row.get("cross_window_stable"))]
    if not filtered:
        return "_None_"
    lines = [
        "| Feature | d validation | d holdout | p validation | p holdout | Stable |",
        "| --- | ---: | ---: | ---: | ---: | :---: |",
    ]
    for row in filtered:
        lines.append(
            "| {feature} | {dv} | {dh} | {pv} | {ph} | {stable} |".format(
                feature=str(row.get("feature") or ""),
                dv=str(row.get("cohens_d_validation") or row.get("cohens_d") or ""),
                dh=str(row.get("cohens_d_holdout") or ""),
                pv=str(row.get("p_value_validation") or row.get("mann_whitney_pvalue") or ""),
                ph=str(row.get("p_value_holdout") or ""),
                stable="YES" if bool(row.get("cross_window_stable")) else "",
            )
        )
    return "\n".join(lines)


def _render_memo(payload: Dict[str, Any]) -> str:
    weighted = list((payload.get("direction_model") or {}).get("top_weighted_features") or [])
    stable_rows = list(payload.get("cross_window_stability") or [])
    stable_count = int(payload.get("stable_feature_count") or 0)
    verdict = str(payload.get("verdict") or "")
    stable_list = ", ".join(str(row.get("feature")) for row in stable_rows if bool(row.get("cross_window_stable")))
    weighted_list = ", ".join(f"{row['feature']} ({row['weight']})" for row in weighted[:5])
    return "\n".join(
        [
            "# Stage 2 Feature Signal Memo",
            "",
            f"- Story: `S2 - Stage 2 feature signal analysis`",
            f"- Run id: `{payload.get('source_run_id')}`",
            f"- Status: `FINAL`",
            "",
            "## Question",
            "",
            "Do the current Stage 2 direction features contain enough cross-window directional signal to justify one bounded retraining cycle?",
            "",
            "## Comparison Set",
            "",
            f"- Scope: `{payload.get('analysis_scope')}`",
            f"- Validation rows: `{((payload.get('validation') or {}).get('rows') or 0)}`",
            f"- Holdout rows: `{((payload.get('holdout') or {}).get('rows') or 0)}`",
            "",
            "## Direction Model Snapshot",
            "",
            f"- Model family: `{((payload.get('direction_model') or {}).get('model_family') or 'unknown')}`",
            f"- Direction features: `{len((payload.get('direction_model') or {}).get('feature_columns') or [])}`",
            f"- Top weighted features: {weighted_list or 'none'}",
            "",
            "## Cross-Window Stability",
            "",
            f"- Stable features: `{stable_count}`",
            f"- Stable feature list: {stable_list or 'none'}",
            "",
            _feature_table_markdown(stable_rows, stable_only=False),
            "",
            "## Decision",
            "",
            verdict,
            "",
            "## Recommended Next Action",
            "",
            "- `Proceed to Story 3` if the decision is YES.",
            "- `Stop or redesign features/target first` if the decision is NO.",
        ]
    )


def run_stage2_feature_signal_diagnostic(
    *,
    run_dir: str | Path,
    fixed_recipe_ids: Sequence[str] = DEFAULT_STAGE2_FEATURE_SIGNAL_FIXED_RECIPE_IDS,
    output_root: str | Path | None = None,
    min_effect_size: float = 0.10,
    max_p_value: float = 0.05,
    min_stable_features: int = 3,
    stage1_positive_only: bool = False,
) -> Dict[str, Any]:
    context = load_stage2_diagnostic_context(
        run_dir=run_dir,
        fixed_recipe_ids=fixed_recipe_ids,
        context_label="stage2 feature signal",
    )
    source_run_dir = context.source_run_dir
    direction_model_report = _extract_weighted_features(context.stage2_package)
    feature_columns = list(direction_model_report.get("feature_columns") or [])

    merged_valid = build_stage2_scored_window_frame(
        context,
        window_name="research_valid",
        include_stage2_feature_columns=feature_columns,
    )
    merged_holdout = build_stage2_scored_window_frame(
        context,
        window_name="final_holdout",
        include_stage2_feature_columns=feature_columns,
    )

    stage1_policy = context.stage1_policy
    entry_threshold = _safe_float(stage1_policy.get("selected_threshold"))
    direction_valid = _direction_oracle_frame(
        merged_valid,
        stage1_positive_only=bool(stage1_positive_only),
        entry_threshold=entry_threshold,
    )
    direction_holdout = _direction_oracle_frame(
        merged_holdout,
        stage1_positive_only=bool(stage1_positive_only),
        entry_threshold=entry_threshold,
    )
    direction_combined = pd.concat([direction_valid, direction_holdout], ignore_index=True)

    validation_separation = _feature_separation(direction_valid, feature_columns)
    holdout_separation = _feature_separation(direction_holdout, feature_columns)
    combined_separation = _feature_separation(direction_combined, feature_columns)
    cross_window_stability = _cross_window_stability(
        validation_separation,
        holdout_separation,
        min_effect_size=float(min_effect_size),
        max_p_value=float(max_p_value),
    )
    stable_feature_count = sum(1 for row in cross_window_stability if bool(row.get("cross_window_stable")))
    signal_exists = bool(stable_feature_count >= int(min_stable_features))
    regime_drift = _regime_drift(direction_valid, direction_holdout, feature_columns)

    analysis_root = Path(output_root).resolve() if output_root is not None else (source_run_dir / "analysis" / "stage2_feature_signal_diagnostic")
    analysis_root.mkdir(parents=True, exist_ok=True)
    summary_output_path = analysis_root / "stage2_feature_signal_summary.json"
    memo_output_path = analysis_root / "stage2_feature_signal_memo.md"

    payload = {
        "analysis_kind": "stage2_feature_signal_diagnostic_v1",
        "created_at_utc": utc_now(),
        "source_run_dir": str(source_run_dir),
        "source_run_id": context.source_run_id,
        "analysis_scope": ("stage1_positive_oracle" if stage1_positive_only else "oracle_positive"),
        "entry_threshold": entry_threshold,
        "criteria": {
            "min_effect_size": float(min_effect_size),
            "max_p_value": float(max_p_value),
            "min_stable_features": int(min_stable_features),
        },
        "direction_model": direction_model_report,
        "validation": {
            "rows": int(len(direction_valid)),
            "ce_rows": int(direction_valid["direction_label"].astype(str).eq("CE").sum()) if len(direction_valid) else 0,
            "pe_rows": int(direction_valid["direction_label"].astype(str).eq("PE").sum()) if len(direction_valid) else 0,
            "feature_separation": validation_separation,
        },
        "holdout": {
            "rows": int(len(direction_holdout)),
            "ce_rows": int(direction_holdout["direction_label"].astype(str).eq("CE").sum()) if len(direction_holdout) else 0,
            "pe_rows": int(direction_holdout["direction_label"].astype(str).eq("PE").sum()) if len(direction_holdout) else 0,
            "feature_separation": holdout_separation,
        },
        "combined": {
            "rows": int(len(direction_combined)),
            "feature_separation": combined_separation,
        },
        "cross_window_stability": cross_window_stability,
        "stable_feature_count": int(stable_feature_count),
        "signal_exists": signal_exists,
        "regime_drift": regime_drift,
        "verdict": (
            "YES - current Stage 2 features contain enough cross-window directional signal to justify one bounded retraining cycle."
            if signal_exists
            else "NO - current Stage 2 features do not contain enough cross-window directional signal to justify retraining as-is."
        ),
        "paths": {
            "analysis_root": str(analysis_root),
            "stage2_feature_signal_summary": str(summary_output_path),
            "stage2_feature_signal_memo": str(memo_output_path),
        },
    }
    summary_output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    memo_output_path.write_text(_render_memo(payload), encoding="utf-8")
    return payload


__all__ = ["run_stage2_feature_signal_diagnostic"]
