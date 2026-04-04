import unittest

from strategy_app.contracts import PositionContext
from strategy_app.engines.regime import Regime
from strategy_app.engines.snapshot_accessor import SnapshotAccessor
from strategy_app.engines.strategies.all_strategies import IVRegimeFilter
from strategy_app.engines.strategy_router import StrategyRouter
from datetime import datetime, timezone


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

    def test_trending_router_excludes_ema_by_default(self) -> None:
        router = StrategyRouter()
        trending_names = [s.name for s in router.get_strategies(Regime.TRENDING, None)]
        exit_names = [s.name for s in router.get_strategies(Regime.TRENDING, object())]
        self.assertNotIn("EMA_CROSSOVER", trending_names)
        self.assertNotIn("EMA_CROSSOVER", exit_names)

    def test_position_exit_router_is_owner_first(self) -> None:
        router = StrategyRouter()
        position = PositionContext(
            position_id="p1",
            direction="CE",
            strike=50000,
            expiry=None,
            entry_premium=100.0,
            entry_time=datetime(2026, 3, 5, 9, 30, tzinfo=timezone.utc),
            entry_snapshot_id="snap-open",
            lots=1,
            entry_strategy="PREV_DAY_LEVEL",
            entry_regime="TRENDING",
        )
        exit_names = [s.name for s in router.get_strategies(Regime.TRENDING, position)]
        self.assertEqual(exit_names, ["PREV_DAY_LEVEL"])


if __name__ == "__main__":
    unittest.main()
