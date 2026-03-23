from __future__ import annotations

from typing import Dict


def build_ml_pure_promotion_ladder(report: Dict[str, object]) -> Dict[str, object]:
    stage_a = report.get("stage_a_predictive_quality") if isinstance(report.get("stage_a_predictive_quality"), dict) else {}
    stage_b = report.get("stage_b_futures_utility") if isinstance(report.get("stage_b_futures_utility"), dict) else {}
    stage_c = report.get("stage_c_option_mapping_diagnostic") if isinstance(report.get("stage_c_option_mapping_diagnostic"), dict) else {}
    promotion_gates = report.get("promotion_gates") if isinstance(report.get("promotion_gates"), dict) else {}
    promotion_eligible = bool(promotion_gates.get("promotion_eligible", False))
    return {
        "lane": "ml_pure",
        "stage_a_passed": bool(stage_a.get("passed", False)),
        "stage_b_passed": bool(stage_b.get("passed", False)),
        "stage_c_non_blocking": bool(stage_c.get("non_blocking", True)),
        "promotion_eligible": promotion_eligible,
        "rationale": "Stage A/B passed; Stage C is diagnostic and non-blocking." if promotion_eligible else "Stage A/B gates not satisfied for ml_pure lane.",
    }


def build_promotion_decision(ml_pure_ladder: Dict[str, object]) -> Dict[str, object]:
    return {
        "primary_lane": "ml_pure",
        "lane": "ml_pure",
        "decision": "PROMOTE" if bool(ml_pure_ladder.get("promotion_eligible")) else "HOLD",
        "rationale": str(ml_pure_ladder.get("rationale") or ""),
        "not_comparable_without_same_holdout_window": True,
    }

