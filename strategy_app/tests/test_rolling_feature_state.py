import unittest
from datetime import date, timedelta

from strategy_app.engines.rolling_feature_state import RollingFeatureState
from strategy_app.engines.snapshot_accessor import SnapshotAccessor


def _snap(
    ts: str,
    close: float,
    volume: float,
    *,
    ce_close: float = 100.0,
    pe_close: float = 100.0,
    vix_current: float = 15.0,
    vix_prev_close: float = 14.0,
    realized_vol_30m: float = 0.01,
) -> SnapshotAccessor:
    return SnapshotAccessor(
        {
            "snapshot_id": ts,
            "session_context": {
                "snapshot_id": ts,
                "timestamp": ts,
                "date": ts[:10],
                "session_phase": "ACTIVE",
                "minutes_since_open": max(0, (int(ts[11:13]) * 60 + int(ts[14:16])) - 555),
                "day_of_week": 0,
                "days_to_expiry": 2,
                "is_expiry_day": False,
            },
            "futures_bar": {
                "fut_open": close - 5.0,
                "fut_high": close + 10.0,
                "fut_low": close - 10.0,
                "fut_close": close,
                "fut_volume": volume,
                "fut_oi": 100000.0,
            },
            "futures_derived": {
                "fut_return_5m": 0.001,
                "fut_return_15m": 0.002,
                "realized_vol_30m": realized_vol_30m,
                "vol_ratio": 1.1,
            },
            "opening_range": {
                "orh": close + 20.0,
                "orl": close - 20.0,
                "orh_broken": False,
                "orl_broken": False,
            },
            "vix_context": {"vix_current": vix_current, "vix_prev_close": vix_prev_close},
            "chain_aggregates": {"total_ce_oi": 1_000_000.0, "total_pe_oi": 900_000.0, "pcr": 0.9},
            "atm_options": {
                "atm_ce_close": ce_close,
                "atm_pe_close": pe_close,
                "atm_ce_volume": 20000.0,
                "atm_pe_volume": 18000.0,
                "atm_ce_oi": 150000.0,
                "atm_pe_oi": 140000.0,
                "atm_ce_iv": 0.16,
                "atm_pe_iv": 0.17,
            },
            "iv_derived": {"iv_skew": -0.01},
        }
    )


class RollingFeatureStateTests(unittest.TestCase):
    def test_features_become_finite_after_warmup(self) -> None:
        state = RollingFeatureState()
        state.on_session_start(date(2026, 3, 2))

        last = None
        for idx in range(30):
            minute = 15 + idx
            ts = f"2026-03-02T09:{minute:02d}:00+05:30"
            snap = _snap(ts, close=50000.0 + idx * 5.0, volume=10000.0 + idx * 200.0)
            last = state.update(snap)

        assert last is not None
        self.assertIsNotNone(last.get("ret_1m"))
        self.assertIsNotNone(last.get("ret_3m"))
        self.assertIsNotNone(last.get("rsi_14"))
        self.assertIsNotNone(last.get("atr_ratio"))
        self.assertIsNotNone(last.get("fut_rel_volume_20"))
        self.assertIsNotNone(last.get("fut_oi_change_1m"))
        self.assertIsNotNone(last.get("fut_oi_change_5m"))
        self.assertIsNotNone(last.get("fut_oi_rel_20"))
        self.assertIsNotNone(last.get("vwap_distance"))
        self.assertIsNotNone(last.get("distance_from_day_high"))
        self.assertIsNotNone(last.get("distance_from_day_low"))

    def test_day_roll_resets_intraday_distance(self) -> None:
        state = RollingFeatureState()
        start = date(2026, 3, 2)
        state.on_session_start(start)

        first_day_last = None
        for idx in range(10):
            minute = 15 + idx
            snap = _snap(f"2026-03-02T09:{minute:02d}:00+05:30", close=50000.0 + idx * 10.0, volume=10000.0)
            first_day_last = state.update(snap)

        state.on_session_end()
        state.on_session_start(start + timedelta(days=1))
        day2_first = state.update(_snap("2026-03-03T09:15:00+05:30", close=51000.0, volume=12000.0))

        assert first_day_last is not None
        self.assertAlmostEqual(float(day2_first["distance_from_day_high"]), (51000.0 - 51010.0) / 51010.0, places=8)
        self.assertAlmostEqual(float(day2_first["distance_from_day_low"]), (51000.0 - 50990.0) / 50990.0, places=8)

    def test_first_bar_and_day_roll_do_not_emit_spurious_atm_oi_change(self) -> None:
        state = RollingFeatureState()
        start = date(2026, 3, 2)
        state.on_session_start(start)

        first = state.update(_snap("2026-03-02T09:15:00+05:30", close=50000.0, volume=10000.0))
        second = state.update(_snap("2026-03-02T09:16:00+05:30", close=50010.0, volume=10100.0))

        state.on_session_end()
        state.on_session_start(start + timedelta(days=1))
        next_day_first = state.update(_snap("2026-03-03T09:15:00+05:30", close=51000.0, volume=12000.0))

        self.assertIsNone(first.get("atm_oi_change_1m"))
        self.assertEqual(float(second["atm_oi_change_1m"]), 0.0)
        self.assertIsNone(next_day_first.get("atm_oi_change_1m"))

    def test_regime_features_follow_training_thresholds(self) -> None:
        state = RollingFeatureState()
        state._daily_atr_history.extend([1.0, 2.0, 3.0, 4.0, 5.0])
        state.on_session_start(date(2026, 3, 2))

        last = None
        for idx in range(20):
            minute = 15 + idx
            last = state.update(
                _snap(
                    f"2026-03-02T09:{minute:02d}:00+05:30",
                    close=50000.0 + idx * 20.0,
                    volume=10000.0 + idx * 100.0,
                    vix_current=19.0,
                    vix_prev_close=15.0,
                    realized_vol_30m=0.0,
                )
            )

        assert last is not None
        self.assertEqual(float(last["regime_vol_high"]), 0.0)
        self.assertEqual(float(last["regime_vol_low"]), 1.0)
        self.assertIsNotNone(last.get("atr_daily_percentile"))
        self.assertGreaterEqual(float(last["atr_daily_percentile"]), 0.70)
        self.assertEqual(float(last["regime_atr_high"]), 1.0)
        self.assertEqual(float(last["regime_atr_low"]), 0.0)


if __name__ == "__main__":
    unittest.main()
