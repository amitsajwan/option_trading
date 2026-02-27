import unittest

import pandas as pd

from ml_pipeline.fill_model import FillModelConfig, estimate_slippage_return, validate_fill_model_config


class FillModelTests(unittest.TestCase):
    def test_spread_fraction_stress(self) -> None:
        cfg = FillModelConfig(
            model="spread_fraction",
            spread_fraction=0.5,
            min_slippage=0.0,
            max_slippage=0.05,
        )
        narrow = pd.Series({"opt_0_ce_close": 100.0, "opt_0_ce_high": 100.2, "opt_0_ce_low": 99.8, "opt_0_ce_volume": 1000.0})
        wide = pd.Series({"opt_0_ce_close": 100.0, "opt_0_ce_high": 102.0, "opt_0_ce_low": 98.0, "opt_0_ce_volume": 1000.0})
        s_narrow = estimate_slippage_return(narrow, side="CE", config=cfg)
        s_wide = estimate_slippage_return(wide, side="CE", config=cfg)
        self.assertGreater(s_wide, s_narrow)

    def test_liquidity_adjusted_volume_impact(self) -> None:
        cfg = FillModelConfig(
            model="liquidity_adjusted",
            spread_fraction=0.3,
            volume_impact_coeff=0.05,
            min_slippage=0.0,
            max_slippage=0.05,
        )
        low_liq = pd.Series({"opt_0_pe_close": 80.0, "opt_0_pe_high": 81.0, "opt_0_pe_low": 79.0, "opt_0_pe_volume": 25.0})
        high_liq = pd.Series({"opt_0_pe_close": 80.0, "opt_0_pe_high": 81.0, "opt_0_pe_low": 79.0, "opt_0_pe_volume": 10000.0})
        s_low = estimate_slippage_return(low_liq, side="PE", config=cfg)
        s_high = estimate_slippage_return(high_liq, side="PE", config=cfg)
        self.assertGreater(s_low, s_high)

    def test_clamp_bounds(self) -> None:
        cfg = FillModelConfig(
            model="constant",
            constant_slippage=0.5,
            min_slippage=0.0,
            max_slippage=0.01,
        )
        row = pd.Series({"opt_0_ce_close": 100.0, "opt_0_ce_high": 101.0, "opt_0_ce_low": 99.0, "opt_0_ce_volume": 100.0})
        value = estimate_slippage_return(row, side="CE", config=cfg)
        self.assertAlmostEqual(value, 0.01, places=12)

    def test_invalid_config(self) -> None:
        bad = FillModelConfig(model="constant", constant_slippage=-0.1)
        with self.assertRaises(ValueError):
            validate_fill_model_config(bad)


if __name__ == "__main__":
    unittest.main()
