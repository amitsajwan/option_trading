"""Unit tests for RiskCalculator (E4-S1 DoD)."""

from __future__ import annotations

import pytest

from strategy_app.risk.risk_calculator import FixedFractionRisk, build_risk_calculator


class TestFixedFractionRisk:
    def test_basic_sizing(self):
        calc = FixedFractionRisk(risk_pct=0.01)
        # capital=500k, entry=1000, stop=4% (0.04), risk=1%
        # risk_capital = 5000
        # max_loss_per_lot = 1000 * 15 * 0.04 = 600
        # lots = floor(5000/600) = 8
        lots = calc.compute_lots(
            entry_premium=1000, stop_loss_pct=0.04, confidence=1.0,
            capital=500_000, max_lots=20,
        )
        assert lots == 8

    def test_minimum_one_lot(self):
        calc = FixedFractionRisk(risk_pct=0.01)
        # Large stop → very few lots → clamps to 1
        lots = calc.compute_lots(
            entry_premium=1000, stop_loss_pct=0.40, confidence=1.0,
            capital=500_000, max_lots=20,
        )
        assert lots == 1

    def test_capped_at_max_lots(self):
        calc = FixedFractionRisk(risk_pct=0.05)
        # Very high risk_pct → many lots → capped at max_lots
        lots = calc.compute_lots(
            entry_premium=100, stop_loss_pct=0.001, confidence=1.0,
            capital=1_000_000, max_lots=5,
        )
        assert lots == 5

    def test_zero_entry_premium_returns_one(self):
        calc = FixedFractionRisk(risk_pct=0.01)
        lots = calc.compute_lots(
            entry_premium=0, stop_loss_pct=0.04, confidence=1.0,
            capital=500_000, max_lots=20,
        )
        assert lots == 1

    def test_zero_stop_loss_returns_one(self):
        calc = FixedFractionRisk(risk_pct=0.01)
        lots = calc.compute_lots(
            entry_premium=1000, stop_loss_pct=0.0, confidence=1.0,
            capital=500_000, max_lots=20,
        )
        assert lots == 1

    def test_name_reflects_pct(self):
        calc = FixedFractionRisk(risk_pct=0.01)
        assert "1.0%" in calc.name

    def test_confidence_param_accepted(self):
        # Confidence is accepted but FixedFraction does not use it (sizing is capital-based)
        calc = FixedFractionRisk(risk_pct=0.01)
        l1 = calc.compute_lots(entry_premium=500, stop_loss_pct=0.04, confidence=0.6, capital=500_000, max_lots=10)
        l2 = calc.compute_lots(entry_premium=500, stop_loss_pct=0.04, confidence=0.9, capital=500_000, max_lots=10)
        assert l1 == l2  # FixedFraction ignores confidence


class TestBuildRiskCalculator:
    def test_default_is_fixed_fraction(self, monkeypatch):
        monkeypatch.delenv("RISK_CALCULATOR", raising=False)
        monkeypatch.delenv("RISK_FRACTION_PCT", raising=False)
        calc = build_risk_calculator()
        assert isinstance(calc, FixedFractionRisk)

    def test_env_override_pct(self, monkeypatch):
        monkeypatch.setenv("RISK_CALCULATOR", "fixed_fraction")
        monkeypatch.setenv("RISK_FRACTION_PCT", "0.02")
        calc = build_risk_calculator()
        assert "2.0%" in calc.name

    def test_unknown_falls_back_to_fixed_fraction(self, monkeypatch):
        monkeypatch.setenv("RISK_CALCULATOR", "unknown_mode")
        calc = build_risk_calculator()
        assert isinstance(calc, FixedFractionRisk)
