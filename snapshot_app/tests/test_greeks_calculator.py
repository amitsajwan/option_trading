from __future__ import annotations

import math

from snapshot_app.core.greeks_calculator import GreeksCalculator


def test_zero_volatility_call_price_uses_discounted_strike_only() -> None:
    price = GreeksCalculator.calculate_option_price(
        spot_price=105.0,
        strike=100.0,
        time_to_expiry=0.5,
        volatility=0.0,
        risk_free_rate=0.07,
        option_type="CE",
    )

    expected = max(0.0, 105.0 - (100.0 * math.exp(-0.07 * 0.5)))
    assert abs(price - expected) < 1e-12


def test_zero_volatility_put_price_uses_discounted_strike_only() -> None:
    price = GreeksCalculator.calculate_option_price(
        spot_price=95.0,
        strike=100.0,
        time_to_expiry=0.5,
        volatility=0.0,
        risk_free_rate=0.07,
        option_type="PE",
    )

    expected = max(0.0, (100.0 * math.exp(-0.07 * 0.5)) - 95.0)
    assert abs(price - expected) < 1e-12
