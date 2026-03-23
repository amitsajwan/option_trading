from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd

from .promotion import build_ml_pure_promotion_ladder, build_promotion_decision
from .stage_metrics import FuturesPromotionGates, positive_rate_diagnostics, stage_a, stage_b, stage_c


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_rate_diagnostics(*, training_frame: Optional[pd.DataFrame], holdout_frame: pd.DataFrame, gap_flag_threshold: float = 0.08) -> Dict[str, object]:
    return positive_rate_diagnostics(training_frame=training_frame, holdout_frame=holdout_frame, gap_flag_threshold=gap_flag_threshold)


def evaluate_futures_stages_from_frame(*, frame: pd.DataFrame, probs: pd.DataFrame, ce_threshold: float, pe_threshold: float, cost_per_trade: float, gates: Optional[FuturesPromotionGates] = None) -> Dict[str, object]:
    cfg = gates or FuturesPromotionGates()
    stage_a_report = stage_a(frame, probs, cfg)
    if not bool(stage_a_report.get("passed")):
        stage_b_report = {"passed": False, "status": "skipped", "reason": "stage_a_failed", "gates": {"futures_pf_min": float(cfg.futures_pf_min), "futures_max_drawdown_pct_max": float(cfg.futures_max_drawdown_pct_max), "futures_trades_min": int(cfg.futures_trades_min), "side_share_min": float(cfg.side_share_min), "side_share_max": float(cfg.side_share_max), "block_rate_min": float(cfg.block_rate_min)}}
    else:
        stage_b_report = stage_b(frame, probs, float(ce_threshold), float(pe_threshold), float(cost_per_trade), cfg)
    stage_c_report = stage_c(frame, stage_b_report)
    # This remains true in the normal path; flipping it false is an operator override that blocks promotion.
    promotion_eligible = bool(stage_a_report.get("passed") and stage_b_report.get("passed") and bool(cfg.no_gate_relaxed_after_results))
    report = {"created_at_utc": _utc_now(), "stage_a_predictive_quality": stage_a_report, "stage_b_futures_utility": stage_b_report, "stage_c_option_mapping_diagnostic": stage_c_report, "promotion_gates": {"no_gate_relaxed_after_results": bool(cfg.no_gate_relaxed_after_results), "promotion_eligible": promotion_eligible}}
    ml_pure = build_ml_pure_promotion_ladder(report)
    report["promotion_ladders"] = {"ml_pure": ml_pure}
    report["promotion_decision"] = build_promotion_decision(ml_pure)
    return report
