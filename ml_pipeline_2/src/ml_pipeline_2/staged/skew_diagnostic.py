from __future__ import annotations

"""
Stage 1+2 CE/PE skew diagnostic.

Reports CE/PE directional breakdown at four levels to identify where
skew enters the pipeline:

  Level 1  raw_oracle         – oracle direction_label before any model scoring
  Level 2  stage1_positive    – rows where entry_prob >= threshold (oracle direction)
  Level 3  stage12_actionable – full CE/PE masked set after Stage 1+2 policy
  Level 4  top_fraction_*     – top-K by ranking_score with side coverage rates

Path interpretation:
  A  oracle is already PE-heavy   → market regime drives skew
  B  oracle balanced, model skewed → Stage 1 or Stage 2 filtering drives skew
  C  actionable balanced, fraction PE-heavy → shared ranking amplifies PE
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

import joblib
import numpy as np
import pandas as pd

from ..experiment_control.state import utc_now
from .confidence_execution import _top_fraction_subset
from .counterfactual import _load_json, _normalize_top_fractions, _resolve_recipe_universe
from .pipeline import (
    KEY_COLUMNS,
    _apply_runtime_filters,
    _build_oracle_targets,
    _load_dataset,
    _merge_policy_inputs,
    _score_single_target,
    _score_stage2_package,
    _stage2_side_masks_from_policy,
    _window,
)
from .registries import view_registry


DEFAULT_SKEW_DIAGNOSTIC_TOP_FRACTIONS: tuple[float, ...] = (0.5, 1.0 / 3.0, 0.25)
DEFAULT_SKEW_DIAGNOSTIC_FIXED_RECIPE_IDS: tuple[str, ...] = ("L3", "L6")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _safe_pct(n: int | float, d: int | float) -> float | None:
    if d == 0:
        return None
    return round(float(n) / float(d), 4)


def _ret_stats(series: pd.Series) -> Dict[str, Any]:
    v = pd.to_numeric(series, errors="coerce").dropna()
    if not len(v):
        return {"n": 0, "mean": None, "max": None, "positive_n": 0, "positive_pct": None}
    return {
        "n": int(len(v)),
        "mean": round(float(v.mean()), 6),
        "max": round(float(v.max()), 6),
        "positive_n": int((v > 0).sum()),
        "positive_pct": _safe_pct(int((v > 0).sum()), int(len(v))),
    }


def _dir_counts(df: pd.DataFrame, col: str) -> Dict[str, Any]:
    total = len(df)
    if total == 0 or col not in df.columns:
        return {"ce": 0, "pe": 0, "ce_pct": None, "other": 0, "total": 0}
    v = df[col].astype(str)
    ce = int((v == "CE").sum())
    pe = int((v == "PE").sum())
    return {"ce": ce, "pe": pe, "ce_pct": _safe_pct(ce, total), "other": total - ce - pe, "total": total}


def _prob_by_side(df: pd.DataFrame, prob_col: str, side_col: str = "selected_side") -> Dict[str, Any]:
    if prob_col not in df.columns or side_col not in df.columns or not len(df):
        return {}
    ce = df[df[side_col].astype(str) == "CE"]
    pe = df[df[side_col].astype(str) == "PE"]
    return {
        "ce_mean": round(float(pd.to_numeric(ce[prob_col], errors="coerce").mean()), 4) if len(ce) else None,
        "pe_mean": round(float(pd.to_numeric(pe[prob_col], errors="coerce").mean()), 4) if len(pe) else None,
    }


def _drop_base_overlap(frame: pd.DataFrame, base_columns: Sequence[str]) -> pd.DataFrame:
    if len(frame.columns) == 0:
        return frame.copy()
    overlap = set(base_columns)
    keep_cols = [col for col in frame.columns if col in KEY_COLUMNS or col not in overlap]
    return frame.loc[:, keep_cols].copy()


def _oracle_level_summary(df: pd.DataFrame, rows_total: int) -> Dict[str, Any]:
    """Summary using oracle direction_label (pre-model levels)."""
    if not len(df):
        return {"total": 0, "rows_total": rows_total}
    ep = (
        df[pd.to_numeric(df["entry_label"], errors="coerce").fillna(0).astype(int) == 1]
        if "entry_label" in df.columns
        else df.iloc[0:0]
    )
    ce_ep = ep[ep["direction_label"].astype(str) == "CE"] if "direction_label" in ep.columns else ep.iloc[0:0]
    pe_ep = ep[ep["direction_label"].astype(str) == "PE"] if "direction_label" in ep.columns else ep.iloc[0:0]
    return {
        "total": len(df),
        "rows_total": rows_total,
        "entry_positive_count": len(ep),
        "oracle_direction": _dir_counts(ep, "direction_label"),
        "oracle_ce_return": _ret_stats(
            ce_ep["best_ce_net_return_after_cost"] if "best_ce_net_return_after_cost" in ce_ep.columns else pd.Series(dtype=float)
        ),
        "oracle_pe_return": _ret_stats(
            pe_ep["best_pe_net_return_after_cost"] if "best_pe_net_return_after_cost" in pe_ep.columns else pd.Series(dtype=float)
        ),
    }


def _model_level_summary(
    df: pd.DataFrame,
    rows_total: int,
    actionable_ce: int,
    actionable_pe: int,
) -> Dict[str, Any]:
    """Summary using selected_side (model-scored levels)."""
    sel = _dir_counts(df, "selected_side")
    ce_rows = df[df["selected_side"].astype(str) == "CE"] if "selected_side" in df.columns and len(df) else df.iloc[0:0]
    pe_rows = df[df["selected_side"].astype(str) == "PE"] if "selected_side" in df.columns and len(df) else df.iloc[0:0]

    agree = None
    if "selected_side" in df.columns and "direction_label" in df.columns and len(df) > 0:
        agree = round(float((df["selected_side"].astype(str) == df["direction_label"].astype(str)).mean()), 4)

    return {
        "total": len(df),
        "rows_total": rows_total,
        "selected_direction": sel,
        "oracle_direction_for_selected": _dir_counts(df, "direction_label"),
        "selected_vs_oracle_agreement": agree,
        "coverage_ce": _safe_pct(sel["ce"], actionable_ce),
        "coverage_pe": _safe_pct(sel["pe"], actionable_pe),
        "selected_side_prob_by_side": _prob_by_side(df, "selected_side_prob"),
        "ranking_score_by_side": _prob_by_side(df, "ranking_score"),
        "oracle_return_ce_selected": _ret_stats(
            ce_rows["best_ce_net_return_after_cost"] if "best_ce_net_return_after_cost" in ce_rows.columns else pd.Series(dtype=float)
        ),
        "oracle_return_pe_selected": _ret_stats(
            pe_rows["best_pe_net_return_after_cost"] if "best_pe_net_return_after_cost" in pe_rows.columns else pd.Series(dtype=float)
        ),
    }


def _get_ce_pct(report: Dict[str, Any], key: str) -> float | None:
    level = report.get(key) or {}
    if key in ("raw_oracle", "stage1_positive"):
        return (level.get("oracle_direction") or {}).get("ce_pct")
    return (level.get("selected_direction") or {}).get("ce_pct")


def _get_top_ce_pct(report: Dict[str, Any], fraction: float) -> float | None:
    for fr in report.get("top_fractions") or []:
        if abs(float(fr.get("fraction", 0)) - fraction) < 0.01:
            return (fr.get("selected_direction") or {}).get("ce_pct")
    return None


def _interpret_window(report: Dict[str, Any]) -> Dict[str, Any]:
    oracle = _get_ce_pct(report, "raw_oracle")
    s1 = _get_ce_pct(report, "stage1_positive")
    s12 = _get_ce_pct(report, "stage12_actionable")
    top25 = _get_top_ce_pct(report, 0.25)

    path = "undetermined"
    note = ""
    if oracle is not None and oracle < 0.35:
        path = "A_market_regime"
        note = f"Oracle is {oracle:.0%} CE. PE dominance originates in market labels before any model."
    elif s1 is not None and oracle is not None and (oracle - s1) > 0.10:
        path = "B_stage1_filtering"
        note = f"Oracle {oracle:.0%} CE but Stage1-positive {s1:.0%} CE. Stage 1 entry gate introduces PE bias."
    elif s12 is not None and s1 is not None and (s1 - s12) > 0.10:
        path = "B_stage2_filtering"
        note = f"Stage1-positive {s1:.0%} CE but Stage1+2 {s12:.0%} CE. Stage 2 direction gate introduces PE bias."
    elif top25 is not None and s12 is not None and (s12 - top25) > 0.10:
        path = "C_ranking_amplification"
        note = f"Stage1+2 actionable {s12:.0%} CE but top-25% {top25:.0%} CE. Shared ranking concentrates PE."
    elif all(x is not None for x in [oracle, s12]) and oracle > 0.35 and s12 is not None and s12 < 0.20:
        path = "B_combined_filtering"
        note = f"Oracle {oracle:.0%} CE, but after Stage1+2 filtering only {s12:.0%} CE. Combined filtering introduces bias."

    return {
        "oracle_ce_pct": oracle,
        "stage1_positive_ce_pct": s1,
        "stage12_actionable_ce_pct": s12,
        "top_25pct_fraction_ce_pct": top25,
        "primary_path": path,
        "note": note,
    }


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------


def run_stage12_skew_diagnostic(
    *,
    run_dir: str | Path,
    top_fractions: Sequence[float] = DEFAULT_SKEW_DIAGNOSTIC_TOP_FRACTIONS,
    fixed_recipe_ids: Sequence[str] = DEFAULT_SKEW_DIAGNOSTIC_FIXED_RECIPE_IDS,
    output_root: str | Path | None = None,
) -> Dict[str, Any]:
    source_run_dir = Path(run_dir).resolve()
    summary = _load_json(source_run_dir / "summary.json")
    resolved_config = _load_json(source_run_dir / "resolved_config.json")
    if str(summary.get("status") or "").strip().lower() != "completed":
        raise ValueError(f"run is not completed: {source_run_dir}")

    normalized_fractions = _normalize_top_fractions(top_fractions)
    normalized_recipe_ids = [str(r).strip() for r in fixed_recipe_ids if str(r).strip()]

    parquet_root = Path(str((resolved_config.get("inputs") or {}).get("parquet_root") or "")).resolve()
    support_dataset = str((resolved_config.get("inputs") or {}).get("support_dataset") or "")
    runtime_block_expiry = bool((resolved_config.get("runtime") or {}).get("block_expiry", False))

    support_raw = _load_dataset(parquet_root, support_dataset)
    support_context = support_raw.loc[:, ~support_raw.columns.duplicated()].copy()
    support_filtered, _ = _apply_runtime_filters(
        support_raw,
        block_expiry=runtime_block_expiry,
        context=f"skew diagnostic support {support_dataset}",
    )

    recipe_universe = _resolve_recipe_universe(
        run_recipe_catalog_id=str(summary.get("recipe_catalog_id") or ""),
        fixed_recipe_ids=normalized_recipe_ids,
    )
    oracle, utility = _build_oracle_targets(
        support_filtered,
        recipe_universe,
        cost_per_trade=float(((resolved_config.get("training") or {}).get("cost_per_trade") or 0.0)),
    )
    # utility carries best_ce/pe_net_return_after_cost as well; oracle already has them.
    # Drop from utility before merging to avoid _x/_y suffixes on those columns.
    _dupe = [c for c in utility.columns if c in set(oracle.columns) - set(["trade_date", "timestamp", "snapshot_id"])]
    utility_base = utility.drop(columns=_dupe) if _dupe else utility
    diagnostic_base = _merge_policy_inputs(oracle, utility_base)
    diagnostic_valid = _window(diagnostic_base, dict((resolved_config.get("windows") or {}).get("research_valid") or {}))
    diagnostic_holdout = _window(
        diagnostic_base,
        dict((resolved_config.get("windows") or {}).get("final_holdout") or {}),
    )

    stage_artifacts = dict(summary.get("stage_artifacts") or {})
    stage1_package = joblib.load(str(((stage_artifacts.get("stage1") or {}).get("model_package_path")) or ""))
    stage2_package = joblib.load(str(((stage_artifacts.get("stage2") or {}).get("model_package_path")) or ""))

    component_ids = dict(summary.get("component_ids") or {})
    stage1_view_id = str(((component_ids.get("stage1") or {}).get("view_id")) or "")
    stage2_view_id = str(((component_ids.get("stage2") or {}).get("view_id")) or "")
    stage1_dataset = view_registry()[stage1_view_id].dataset_name
    stage2_dataset = view_registry()[stage2_view_id].dataset_name

    s1_raw = _load_dataset(parquet_root, stage1_dataset)
    s2_raw = _load_dataset(parquet_root, stage2_dataset)
    s1_filtered, _ = _apply_runtime_filters(
        s1_raw,
        block_expiry=runtime_block_expiry,
        support_context=support_context,
        context="skew diagnostic stage1",
    )
    s2_filtered, _ = _apply_runtime_filters(
        s2_raw,
        block_expiry=runtime_block_expiry,
        support_context=support_context,
        context="skew diagnostic stage2",
    )

    stage1_policy = dict((summary.get("policy_reports") or {}).get("stage1") or {})
    stage2_policy = dict((summary.get("policy_reports") or {}).get("stage2") or {})
    if "selected_threshold" not in stage1_policy:
        raise ValueError("stage1 selected_threshold missing from summary.policy_reports")
    entry_threshold = float(stage1_policy["selected_threshold"])

    def _report_for_window(window_name: str, diagnostic_window: pd.DataFrame) -> Dict[str, Any]:
        rows_total = int(len(diagnostic_window))
        window_cfg = dict((resolved_config.get("windows") or {}).get(window_name) or {})

        s1_win = _window(s1_filtered, window_cfg)
        s2_win = _window(s2_filtered, window_cfg)
        s1_scores = _drop_base_overlap(
            _score_single_target(s1_win, stage1_package, prob_col="entry_prob"),
            diagnostic_window.columns,
        )
        s2_scores = _drop_base_overlap(
            _score_stage2_package(s2_win, stage2_package),
            diagnostic_window.columns,
        )
        merged = _merge_policy_inputs(diagnostic_window, s1_scores, s2_scores)

        # Level 1: raw oracle
        l1 = _oracle_level_summary(diagnostic_window, rows_total)

        # Level 2: Stage 1 positive (entry_prob >= threshold, oracle direction)
        entry_prob_arr = pd.to_numeric(
            merged["entry_prob"] if "entry_prob" in merged.columns else pd.Series([0.0] * len(merged), index=merged.index),
            errors="coerce",
        ).fillna(0.0)
        l2_frame = merged.loc[entry_prob_arr >= entry_threshold].copy()
        l2 = _oracle_level_summary(l2_frame, rows_total)

        # Level 3: Stage 1+2 actionable — replicate _selected_stage12_trades logic
        direction_available = pd.to_numeric(
            merged["direction_up_prob"] if "direction_up_prob" in merged.columns
            else pd.Series([float("nan")] * len(merged), index=merged.index),
            errors="coerce",
        ).notna().to_numpy(dtype=bool, copy=False)

        ce_mask, pe_mask = _stage2_side_masks_from_policy(
            merged,
            entry_threshold=entry_threshold,
            stage2_policy=stage2_policy,
        )
        ce_mask = ce_mask & direction_available
        pe_mask = pe_mask & direction_available
        trade_mask = ce_mask | pe_mask

        l3_frame = merged.loc[trade_mask].copy()
        if len(l3_frame) > 0:
            l3_frame["selected_side"] = np.where(ce_mask[trade_mask], "CE", "PE")
            ep_v = pd.to_numeric(l3_frame["entry_prob"] if "entry_prob" in l3_frame.columns else pd.Series([0.0] * len(l3_frame)), errors="coerce").fillna(0.0)
            tp_v = pd.to_numeric(l3_frame["direction_trade_prob"] if "direction_trade_prob" in l3_frame.columns else pd.Series([1.0] * len(l3_frame)), errors="coerce").fillna(1.0)
            dup_v = pd.to_numeric(l3_frame["direction_up_prob"] if "direction_up_prob" in l3_frame.columns else pd.Series([0.5] * len(l3_frame)), errors="coerce").fillna(0.5)
            l3_frame["selected_side_prob"] = np.where(l3_frame["selected_side"] == "CE", dup_v, 1.0 - dup_v)
            l3_frame["ranking_score"] = ep_v * tp_v * l3_frame["selected_side_prob"]
            l3_frame = l3_frame.sort_values(
                by=["ranking_score", "selected_side_prob", "entry_prob", "timestamp"],
                ascending=[False, False, False, True],
            ).reset_index(drop=True)

        actionable_ce = int((l3_frame["selected_side"] == "CE").sum()) if "selected_side" in l3_frame.columns else 0
        actionable_pe = int((l3_frame["selected_side"] == "PE").sum()) if "selected_side" in l3_frame.columns else 0
        l3 = _model_level_summary(l3_frame, rows_total, actionable_ce, actionable_pe)

        # Level 4: top-K fractions
        fraction_reports: List[Dict[str, Any]] = []
        for fraction in normalized_fractions:
            subset, keep_count, score_floor = _top_fraction_subset(l3_frame, fraction)
            report = _model_level_summary(subset, rows_total, actionable_ce, actionable_pe)
            report["fraction"] = float(fraction)
            report["keep_count"] = int(keep_count)
            report["score_floor"] = float(score_floor) if score_floor != float("-inf") else None
            fraction_reports.append(report)

        return {
            "window": window_name,
            "rows_total": rows_total,
            "entry_threshold": entry_threshold,
            "raw_oracle": l1,
            "stage1_positive": l2,
            "stage12_actionable": l3,
            "top_fractions": fraction_reports,
        }

    valid_report = _report_for_window("research_valid", diagnostic_valid)
    holdout_report = _report_for_window("final_holdout", diagnostic_holdout)

    interpretation = {
        "research_valid": _interpret_window(valid_report),
        "final_holdout": _interpret_window(holdout_report),
    }

    analysis_root = (
        Path(output_root).resolve()
        if output_root is not None
        else (source_run_dir / "analysis" / "stage12_skew_diagnostic")
    )
    analysis_root.mkdir(parents=True, exist_ok=True)
    output_path = analysis_root / "skew_diagnostic.json"

    payload: Dict[str, Any] = {
        "analysis_kind": "stage12_skew_diagnostic_v1",
        "created_at_utc": utc_now(),
        "source_run_dir": str(source_run_dir),
        "source_run_id": str(summary.get("run_id") or source_run_dir.name),
        "top_fractions": normalized_fractions,
        "fixed_recipe_ids": normalized_recipe_ids,
        "entry_threshold": entry_threshold,
        "interpretation": interpretation,
        "research_valid": valid_report,
        "final_holdout": holdout_report,
        "paths": {
            "analysis_root": str(analysis_root),
            "skew_diagnostic": str(output_path),
        },
    }

    output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


__all__ = [
    "DEFAULT_SKEW_DIAGNOSTIC_FIXED_RECIPE_IDS",
    "DEFAULT_SKEW_DIAGNOSTIC_TOP_FRACTIONS",
    "run_stage12_skew_diagnostic",
]
