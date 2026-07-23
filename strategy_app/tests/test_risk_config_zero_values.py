"""PositionRiskConfig/StrategyTrailConfig.from_payload must preserve a
deliberately configured 0.0, not silently substitute the hardcoded default
(found 2026-07-22 review): `float(as_optional_float(x) or default)` treats
0.0 as falsy, so a config change believed to zero out a gate had no effect.
"""
from __future__ import annotations

from strategy_app.risk.config import PositionRiskConfig, StrategyTrailConfig


def test_trail_config_preserves_explicit_zero_activation_mfe():
    cfg = StrategyTrailConfig.from_payload({"activation_mfe": 0.0})
    assert cfg.activation_mfe == 0.0


def test_trail_config_uses_default_when_absent():
    cfg = StrategyTrailConfig.from_payload({})
    assert cfg.activation_mfe == 0.15


def test_position_risk_config_preserves_explicit_zero_trailing_activation():
    cfg = PositionRiskConfig.from_payload({"trailing_activation_pct": 0.0})
    assert cfg.trailing_activation_pct == 0.0


def test_position_risk_config_preserves_explicit_zero_stagnant_min_gain():
    cfg = PositionRiskConfig.from_payload({"stagnant_min_gain_pct": 0.0})
    assert cfg.stagnant_min_gain_pct == 0.0


def test_position_risk_config_preserves_explicit_zero_thesis_fail_pnl():
    cfg = PositionRiskConfig.from_payload({"thesis_fail_pnl_pct": 0.0})
    assert cfg.thesis_fail_pnl_pct == 0.0


def test_position_risk_config_defaults_when_absent():
    cfg = PositionRiskConfig.from_payload({})
    assert cfg.trailing_activation_pct == 0.10
    assert cfg.stagnant_min_gain_pct == 0.05
    assert cfg.thesis_fail_pnl_pct == -0.08
