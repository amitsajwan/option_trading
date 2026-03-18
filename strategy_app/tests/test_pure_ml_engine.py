import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from strategy_app.contracts import ExitReason, SignalType
from strategy_app.engines.pure_ml_engine import PureMLEngine
from strategy_app.logging.signal_logger import SignalLogger


class _ConstantProbModel:
    def __init__(self, prob: float) -> None:
        self._prob = float(prob)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = int(len(x))
        p1 = np.full(shape=(n,), fill_value=self._prob, dtype=float)
        p0 = 1.0 - p1
        return np.column_stack([p0, p1])


def _snapshot(
    *,
    snapshot_id: str,
    ts: str,
    ce_ltp: float = 100.0,
    pe_ltp: float = 100.0,
    ce_oi: float = 120000.0,
    pe_oi: float = 120000.0,
    ce_volume: float = 20000.0,
    pe_volume: float = 20000.0,
    session_phase: str = "ACTIVE",
    r5m: float = 0.01,
    r15m: float = 0.015,
    r30m: float = 0.02,
    vol_ratio: float = 1.5,
    orh_broken: bool = True,
    orl_broken: bool = False,
    is_expiry_day: bool = False,
    days_to_expiry: int = 2,
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "session_context": {
            "snapshot_id": snapshot_id,
            "timestamp": ts,
            "date": ts[:10],
            "session_phase": session_phase,
            "days_to_expiry": days_to_expiry,
            "is_expiry_day": is_expiry_day,
        },
        "futures_derived": {
            "fut_return_5m": r5m,
            "fut_return_15m": r15m,
            "fut_return_30m": r30m,
            "realized_vol_30m": 0.01,
            "vol_ratio": vol_ratio,
            "fut_oi_change_30m": 1200.0,
            "fut_oi": 100000.0,
            "fut_volume_ratio": 1.3,
            "price_vs_vwap": 0.001,
            "ema_9": 50010.0,
            "ema_21": 50000.0,
        },
        "opening_range": {
            "orh_broken": bool(orh_broken),
            "orl_broken": bool(orl_broken),
            "or_width": 100.0,
            "price_vs_orh": 0.005,
            "price_vs_orl": 0.015,
        },
        "vix_context": {
            "vix_current": 15.0,
            "vix_prev_close": 14.5,
            "vix_intraday_chg": 2.0,
            "vix_spike_flag": False,
        },
        "chain_aggregates": {
            "atm_strike": 50000,
            "total_ce_oi": 1_000_000.0,
            "total_pe_oi": 900_000.0,
            "pcr": 1.25,
        },
        "atm_options": {
            "atm_ce_close": ce_ltp,
            "atm_pe_close": pe_ltp,
            "atm_ce_volume": ce_volume,
            "atm_pe_volume": pe_volume,
            "atm_ce_oi": ce_oi,
            "atm_pe_oi": pe_oi,
            "atm_ce_iv": 0.16,
            "atm_pe_iv": 0.17,
            "atm_ce_vol_ratio": 1.1,
            "atm_pe_vol_ratio": 1.0,
            "atm_ce_oi_change_30m": 8000.0,
            "atm_pe_oi_change_30m": 4000.0,
        },
        "iv_derived": {"iv_skew": -0.01},
        "strikes": [
            {
                "strike": 50000.0,
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_volume": ce_volume,
                "pe_volume": pe_volume,
            }
        ],
    }


def _flat_snapshot(
    *,
    snapshot_id: str,
    ts: str,
    ce_ltp: float = 100.0,
    pe_ltp: float = 100.0,
    ce_oi: float = 120000.0,
    pe_oi: float = 120000.0,
    ce_volume: float = 20000.0,
    pe_volume: float = 20000.0,
    r5m: float = 0.01,
) -> dict[str, object]:
    return {
        "trade_date": ts[:10],
        "timestamp": ts,
        "snapshot_id": snapshot_id,
        "schema_name": "SnapshotMLFlat",
        "schema_version": "1.0.0",
        "build_source": "live",
        "build_run_id": "test",
        "px_fut_open": 50000.0,
        "px_fut_high": 50020.0,
        "px_fut_low": 49980.0,
        "px_fut_close": 50010.0,
        "ret_1m": 0.001,
        "ret_3m": 0.002,
        "ret_5m": r5m,
        "ema_9": 50010.0,
        "ema_21": 50000.0,
        "ema_50": 49990.0,
        "ema_9_21_spread": 10.0,
        "osc_rsi_14": 58.0,
        "osc_atr_14": 50.0,
        "osc_atr_ratio": 0.01,
        "osc_atr_percentile": 0.6,
        "osc_atr_daily_percentile": 0.7,
        "vwap_distance": 0.001,
        "dist_from_day_high": -0.001,
        "dist_from_day_low": 0.002,
        "fut_flow_volume": 120000.0,
        "fut_flow_oi": 250000.0,
        "fut_flow_rel_volume_20": 1.3,
        "opt_flow_atm_strike": 50000,
        "opt_flow_ce_oi_total": 1_000_000.0,
        "opt_flow_pe_oi_total": 900_000.0,
        "opt_flow_ce_volume_total": 90_000.0,
        "opt_flow_pe_volume_total": 80_000.0,
        "opt_flow_pcr_oi": 1.25,
        "opt_flow_atm_oi_change_1m": 12_000.0,
        "opt_flow_ce_pe_oi_diff": 100_000.0,
        "opt_flow_ce_pe_volume_diff": 10_000.0,
        "opt_flow_options_volume_total": 170_000.0,
        "time_minute_of_day": 570,
        "time_day_of_week": 0,
        "time_minute_index": 15,
        "ctx_opening_range_ready": 1.0,
        "ctx_opening_range_breakout_up": 1.0,
        "ctx_opening_range_breakout_down": 0.0,
        "ctx_dte_days": 2.0,
        "ctx_is_expiry_day": 0.0,
        "ctx_is_near_expiry": 0.0,
        "ctx_is_high_vix_day": 0.0,
        "ctx_regime_atr_high": 0.0,
        "ctx_regime_atr_low": 0.0,
        "ctx_regime_trend_up": 1.0,
        "ctx_regime_trend_down": 0.0,
        "ctx_regime_expiry_near": 0.0,
        "fut_return_15m": 0.015,
        "fut_return_30m": 0.020,
        "realized_vol_30m": 0.010,
        "vol_ratio": 1.4,
        "fut_oi_change_30m": 1500.0,
        "atm_ce_close": ce_ltp,
        "atm_pe_close": pe_ltp,
        "atm_ce_volume": ce_volume,
        "atm_pe_volume": pe_volume,
        "atm_ce_oi": ce_oi,
        "atm_pe_oi": pe_oi,
        "session_context": {
            "snapshot_id": snapshot_id,
            "timestamp": ts,
            "date": ts[:10],
            "session_phase": "ACTIVE",
            "days_to_expiry": 2,
            "is_expiry_day": False,
            "minutes_since_open": 15,
            "day_of_week": 0,
        },
        "futures_derived": {
            "fut_return_5m": r5m,
            "fut_return_15m": 0.015,
            "fut_return_30m": 0.020,
            "realized_vol_30m": 0.010,
            "vol_ratio": 1.4,
            "fut_oi_change_30m": 1500.0,
            "fut_volume_ratio": 1.3,
            "price_vs_vwap": 0.001,
            "ema_9": 50010.0,
            "ema_21": 50000.0,
        },
        "opening_range": {
            "orh_broken": True,
            "orl_broken": False,
        },
        "vix_context": {
            "vix_current": 15.0,
            "vix_prev_close": 14.5,
            "vix_intraday_chg": 2.0,
            "vix_spike_flag": False,
        },
        "chain_aggregates": {
            "atm_strike": 50000,
            "total_ce_oi": 1_000_000.0,
            "total_pe_oi": 900_000.0,
            "pcr": 1.25,
        },
        "atm_options": {
            "atm_ce_close": ce_ltp,
            "atm_pe_close": pe_ltp,
            "atm_ce_volume": ce_volume,
            "atm_pe_volume": pe_volume,
            "atm_ce_oi": ce_oi,
            "atm_pe_oi": pe_oi,
        },
        "strikes": [
            {
                "strike": 50000.0,
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
                "ce_oi": ce_oi,
                "pe_oi": pe_oi,
                "ce_volume": ce_volume,
                "pe_volume": pe_volume,
            }
        ],
    }


class PureMLEngineTests(unittest.TestCase):
    def _write_model_bundle(
        self,
        root: Path,
        *,
        ce_prob: float,
        pe_prob: float,
        feature_columns: list[str] | None = None,
    ) -> Path:
        path = root / "model.joblib"
        bundle = {
            "feature_columns": feature_columns or ["ret_5m", "pcr_oi", "minute_of_day"],
            "models": {"ce": _ConstantProbModel(ce_prob), "pe": _ConstantProbModel(pe_prob)},
        }
        joblib.dump(bundle, path)
        return path

    def _write_threshold_report(self, root: Path, *, ce_threshold: float, pe_threshold: float, block_expiry: bool = False) -> Path:
        path = root / "thresholds.json"
        payload = {
            "ce_threshold": ce_threshold,
            "pe_threshold": pe_threshold,
            "runtime": {"block_expiry": bool(block_expiry)},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _build_engine(
        self,
        *,
        root: Path,
        ce_prob: float,
        pe_prob: float,
        ce_threshold: float = 0.60,
        pe_threshold: float = 0.60,
        max_hold_bars: int = 15,
        min_edge: float = 0.0,
        max_feature_age_sec: int = 10_000_000,
        max_nan_features: int = 3,
        feature_columns: list[str] | None = None,
        block_expiry: bool = False,
    ) -> PureMLEngine:
        model_path = self._write_model_bundle(
            root,
            ce_prob=ce_prob,
            pe_prob=pe_prob,
            feature_columns=feature_columns,
        )
        threshold_path = self._write_threshold_report(root, ce_threshold=ce_threshold, pe_threshold=pe_threshold, block_expiry=block_expiry)
        return PureMLEngine(
            model_package_path=str(model_path),
            threshold_report_path=str(threshold_path),
            min_confidence=0.0,
            min_edge=min_edge,
            max_feature_age_sec=max_feature_age_sec,
            max_nan_features=max_nan_features,
            max_hold_bars=max_hold_bars,
            signal_logger=SignalLogger(root),
        )

    def test_ce_only_pass_emits_buy_ce(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.80, pe_prob=0.20)
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNotNone(signal)
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            self.assertEqual(signal.direction, "CE")
            self.assertEqual(signal.source, "ML_PURE")
            self.assertEqual(signal.entry_strategy_name, "ML_PURE_DUAL")
            self.assertEqual(signal.max_hold_bars, 15)
            self.assertAlmostEqual(float(signal.confidence or 0.0), 0.80, places=6)

    def test_pe_only_pass_emits_buy_pe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.30, pe_prob=0.75)
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNotNone(signal)
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            self.assertEqual(signal.direction, "PE")

    def test_both_pass_picks_higher_probability_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.72, pe_prob=0.81)
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNotNone(signal)
            self.assertEqual(signal.direction, "PE")

    def test_both_pass_tie_breaks_to_ce(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.70, pe_prob=0.70)
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNotNone(signal)
            self.assertEqual(signal.direction, "CE")

    def test_neither_pass_returns_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.30, pe_prob=0.35, ce_threshold=0.60, pe_threshold=0.60)
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNone(signal)

    def test_low_edge_conflict_holds_when_both_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.70, pe_prob=0.65, min_edge=0.15)
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNone(signal)

    def test_sideways_regime_blocks_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.80, pe_prob=0.20)
            engine.on_session_start(date(2026, 3, 2))
            snap = _snapshot(
                snapshot_id="snap-sideways",
                ts="2026-03-02T09:30:00+05:30",
                r5m=0.0002,
                r15m=0.0001,
                r30m=0.0001,
                vol_ratio=0.95,
                orh_broken=False,
                orl_broken=False,
            )

            signal = engine.evaluate(snap)

            self.assertIsNone(signal)

    def test_liquidity_gate_blocks_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.80, pe_prob=0.20)
            engine.on_session_start(date(2026, 3, 2))
            snap = _snapshot(
                snapshot_id="snap-illiquid",
                ts="2026-03-02T09:30:00+05:30",
                ce_oi=3000.0,
                ce_volume=500.0,
            )

            signal = engine.evaluate(snap)

            self.assertIsNone(signal)

    def test_position_exits_on_max_hold_bars_time_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.80, pe_prob=0.20, max_hold_bars=3)
            engine.on_session_start(date(2026, 3, 2))

            open_signal = engine.evaluate(_snapshot(snapshot_id="snap-1", ts="2026-03-02T09:30:00+05:30"))
            self.assertIsNotNone(open_signal)
            self.assertEqual(open_signal.signal_type, SignalType.ENTRY)

            hold_1 = engine.evaluate(_snapshot(snapshot_id="snap-2", ts="2026-03-02T09:31:00+05:30"))
            hold_2 = engine.evaluate(_snapshot(snapshot_id="snap-3", ts="2026-03-02T09:32:00+05:30"))
            exit_signal = engine.evaluate(_snapshot(snapshot_id="snap-4", ts="2026-03-02T09:33:00+05:30"))

            self.assertIsNone(hold_1)
            self.assertIsNone(hold_2)
            self.assertIsNotNone(exit_signal)
            self.assertEqual(exit_signal.signal_type, SignalType.EXIT)
            self.assertEqual(exit_signal.exit_reason, ExitReason.TIME_STOP)

    def test_stale_snapshot_blocks_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.80, pe_prob=0.20, max_feature_age_sec=1)
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(_snapshot(snapshot_id="snap-old", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNone(signal)

    def test_feature_incomplete_blocks_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(
                root=root,
                ce_prob=0.80,
                pe_prob=0.20,
                feature_columns=["ret_5m", "unknown_a", "unknown_b", "unknown_c", "unknown_d", "unknown_e"],
                max_nan_features=2,
            )
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(_snapshot(snapshot_id="snap-missing", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNone(signal)

    def test_flat_snapshot_ml_v1_payload_can_drive_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(
                root=root,
                ce_prob=0.82,
                pe_prob=0.18,
                feature_columns=["ret_5m", "opt_flow_pcr_oi", "time_minute_of_day", "dealer_proxy_oi_imbalance"],
            )
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(_flat_snapshot(snapshot_id="flat-1", ts="2026-03-02T09:30:00+05:30"))

            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            self.assertEqual(signal.direction, "CE")

    def test_set_run_context_applies_regime_config_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.80, pe_prob=0.20)
            baseline = float(engine._regime._trend_return_min)

            engine.set_run_context("run-1", {"regime_config": {"trend_return_min": 0.25}})

            self.assertNotEqual(baseline, float(engine._regime._trend_return_min))
            self.assertEqual(float(engine._regime._trend_return_min), 0.25)

    def test_dual_engine_can_block_expiry_via_threshold_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.80, pe_prob=0.20, block_expiry=True)
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(
                _snapshot(
                    snapshot_id="snap-expiry",
                    ts="2026-03-02T09:30:00+05:30",
                    is_expiry_day=True,
                    days_to_expiry=0,
                )
            )

            self.assertIsNone(signal)

    def test_dual_engine_default_runtime_controls_do_not_block_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine = self._build_engine(root=root, ce_prob=0.80, pe_prob=0.20, block_expiry=False)
            engine.on_session_start(date(2026, 3, 2))

            signal = engine.evaluate(
                _snapshot(
                    snapshot_id="snap-expiry-allowed",
                    ts="2026-03-02T09:30:00+05:30",
                    is_expiry_day=True,
                    days_to_expiry=0,
                )
            )

            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertEqual(signal.signal_type, SignalType.ENTRY)
            self.assertEqual(signal.direction, "CE")


if __name__ == "__main__":
    unittest.main()
