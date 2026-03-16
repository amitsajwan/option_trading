import unittest

from strategy_app.engines.regime import Regime
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.strategies.all_strategies import IVRegimeFilter
from strategy_app.engines.strategy_router import StrategyRouter


def _snapshot(*, vix_intraday_chg: float | None, vix_spike_flag: bool, iv_percentile: float | None) -> dict:
    return {
        "session_context": {
            "snapshot_id": "snap-1",
            "timestamp": "2026-03-05T10:00:00+05:30",
            "date": "2026-03-05",
            "session_phase": "ACTIVE",
        },
        "vix_context": {
            "vix_intraday_chg": vix_intraday_chg,
            "vix_spike_flag": vix_spike_flag,
        },
        "iv_derived": {
            "iv_percentile": iv_percentile,
            "iv_regime": "NEUTRAL",
        },
    }


class IVFilterAndRouterTests(unittest.TestCase):
    def test_iv_filter_vetoes_spiking(self) -> None:
        strat = IVRegimeFilter()
        vote = strat.evaluate(_snapshot(vix_intraday_chg=16.0, vix_spike_flag=False, iv_percentile=70.0), None, None)  # type: ignore[arg-type]
        self.assertIsNotNone(vote)
        self.assertEqual(vote.signal_type.value, "SKIP")

    def test_iv_filter_allows_elevated_stable(self) -> None:
        strat = IVRegimeFilter()
        vote = strat.evaluate(_snapshot(vix_intraday_chg=4.0, vix_spike_flag=False, iv_percentile=88.0), None, None)  # type: ignore[arg-type]
        self.assertIsNone(vote)

    def test_iv_filter_vetoes_extreme_iv(self) -> None:
        strat = IVRegimeFilter()
        vote = strat.evaluate(_snapshot(vix_intraday_chg=4.0, vix_spike_flag=False, iv_percentile=96.0), None, None)  # type: ignore[arg-type]
        self.assertIsNotNone(vote)
        self.assertIn("extreme_iv_percentile", vote.reason)

    def test_high_vol_router_contains_high_vol_orb_only_in_high_vol(self) -> None:
        router = StrategyRouter()
        high_vol_names = [s.name for s in router.get_strategies(Regime.HIGH_VOL, None)]
        trending_names = [s.name for s in router.get_strategies(Regime.TRENDING, None)]
        self.assertIn("HIGH_VOL_ORB", high_vol_names)
        self.assertNotIn("HIGH_VOL_ORB", trending_names)


if __name__ == "__main__":
    unittest.main()
