import unittest

import numpy as np
import pandas as pd

from snapshot_app.core.runtime_features import _add_group_features
from strategy_app.engines.rolling_feature_state import RollingFeatureState
from strategy_app.engines.snapshot_accessor import SnapshotAccessor


def _build_snapshot_from_row(row: pd.Series) -> SnapshotAccessor:
    ts = pd.Timestamp(row["timestamp"]).isoformat()
    return SnapshotAccessor(
        {
            "snapshot_id": str(row["timestamp"]),
            "session_context": {
                "snapshot_id": str(row["timestamp"]),
                "timestamp": ts,
                "date": str(row["trade_date"]),
                "session_phase": "ACTIVE",
                "minutes_since_open": int((pd.Timestamp(row["timestamp"]).hour * 60 + pd.Timestamp(row["timestamp"]).minute) - 555),
                "day_of_week": int(pd.Timestamp(row["timestamp"]).dayofweek),
                "days_to_expiry": 2,
                "is_expiry_day": False,
            },
            "futures_bar": {
                "fut_open": float(row["fut_open"]),
                "fut_high": float(row["fut_high"]),
                "fut_low": float(row["fut_low"]),
                "fut_close": float(row["fut_close"]),
                "fut_volume": float(row["fut_volume"]),
                "fut_oi": float(row["fut_oi"]),
            },
            "futures_derived": {
                "fut_return_5m": float(row.get("ret_5m")) if pd.notna(row.get("ret_5m")) else None,
                "fut_return_15m": 0.0,
                "realized_vol_30m": 0.01,
                "vol_ratio": 1.0,
            },
            "opening_range": {
                "orh": float(row.get("opening_range_high")) if pd.notna(row.get("opening_range_high")) else None,
                "orl": float(row.get("opening_range_low")) if pd.notna(row.get("opening_range_low")) else None,
                "orh_broken": bool(row.get("opening_range_breakout_up") == 1),
                "orl_broken": bool(row.get("opening_range_breakout_down") == 1),
            },
            "vix_context": {"vix_current": 15.0, "vix_prev_close": 14.5},
            "chain_aggregates": {
                "atm_strike": 50000,
                "pcr": float(row["pcr_oi"]),
                "total_ce_oi": float(row["ce_oi_total"]),
                "total_pe_oi": float(row["pe_oi_total"]),
            },
            "atm_options": {
                "atm_ce_close": float(row["opt_0_ce_close"]),
                "atm_pe_close": float(row["opt_0_pe_close"]),
                "atm_ce_volume": float(row["ce_volume_total"]) / 2.0,
                "atm_pe_volume": float(row["pe_volume_total"]) / 2.0,
                "atm_ce_oi": float(row["opt_0_ce_oi"]),
                "atm_pe_oi": float(row["opt_0_pe_oi"]),
                "atm_ce_iv": 0.16,
                "atm_pe_iv": 0.17,
            },
            "iv_derived": {"iv_skew": -0.01},
            "strikes": [
                {
                    "strike": 49900,
                    "ce_oi": float(row["opt_m1_ce_oi"]),
                    "pe_oi": float(row["opt_m1_pe_oi"]),
                },
                {
                    "strike": 50000,
                    "ce_oi": float(row["opt_0_ce_oi"]),
                    "pe_oi": float(row["opt_0_pe_oi"]),
                },
                {
                    "strike": 50100,
                    "ce_oi": float(row["opt_p1_ce_oi"]),
                    "pe_oi": float(row["opt_p1_pe_oi"]),
                },
            ],
        }
    )


class FeatureParityBatchVsStreamTests(unittest.TestCase):
    def test_parity_for_core_streamable_features(self) -> None:
        n = 80
        ts = pd.date_range("2026-03-02 09:15:00+05:30", periods=n, freq="min")
        base = 50000.0 + np.linspace(0.0, 120.0, n) + 8.0 * np.sin(np.arange(n) / 5.0)
        panel = pd.DataFrame(
            {
                "timestamp": ts,
                "trade_date": ["2026-03-02"] * n,
                "fut_open": base - 2.0,
                "fut_high": base + 6.0,
                "fut_low": base - 6.0,
                "fut_close": base,
                "fut_volume": 10000.0 + 250.0 * np.arange(n),
                "fut_oi": 100000.0 + 50.0 * np.arange(n),
                "spot_close": base - 20.0,
                "opt_0_ce_close": 100.0 + 0.5 * np.arange(n),
                "opt_0_pe_close": 110.0 - 0.4 * np.arange(n),
                "opt_0_ce_oi": 200000.0 + 100.0 * np.arange(n),
                "opt_0_pe_oi": 180000.0 + 80.0 * np.arange(n),
                "opt_m1_ce_oi": 195000.0 + 95.0 * np.arange(n),
                "opt_m1_pe_oi": 175000.0 + 70.0 * np.arange(n),
                "opt_p1_ce_oi": 190000.0 + 90.0 * np.arange(n),
                "opt_p1_pe_oi": 170000.0 + 65.0 * np.arange(n),
                "ce_oi_total": 1_200_000.0 + 300.0 * np.arange(n),
                "pe_oi_total": 1_100_000.0 + 240.0 * np.arange(n),
                "ce_volume_total": 250000.0 + 500.0 * np.arange(n),
                "pe_volume_total": 220000.0 + 450.0 * np.arange(n),
                "opt_0_ce_iv": np.full(n, 0.16),
                "opt_0_pe_iv": np.full(n, 0.17),
                "pcr_oi": 0.92 + (0.002 * np.arange(n)),
            }
        )
        batch = _add_group_features(panel.copy())

        state = RollingFeatureState()
        state.on_session_start(pd.Timestamp("2026-03-02").date())
        stream_rows = []
        for _, row in batch.iterrows():
            snap = _build_snapshot_from_row(row)
            stream_rows.append(state.update(snap))
        stream = pd.DataFrame(stream_rows)

        cols = [
            "ret_1m",
            "ret_3m",
            "ret_5m",
            "ema_9_21_spread",
            "ema_9_slope",
            "ema_21_slope",
            "ema_50_slope",
            "rsi_14",
            "atr_ratio",
            "vwap_distance",
            "distance_from_day_high",
            "distance_from_day_low",
            "fut_rel_volume_20",
            "fut_volume_accel_1m",
            "fut_oi_change_1m",
            "fut_oi_change_5m",
            "fut_oi_rel_20",
            "fut_oi_zscore_20",
            "pcr_change_5m",
            "pcr_change_15m",
            "atm_oi_ratio",
            "near_atm_oi_ratio",
        ]

        for col in cols:
            left = pd.to_numeric(batch[col], errors="coerce")
            right = pd.to_numeric(stream[col], errors="coerce")
            mask = left.notna() & right.notna()
            # skip warmup rows; compare on stable region
            idx = np.where(mask.to_numpy())[0]
            idx = idx[idx >= 25]
            if len(idx) == 0:
                continue
            np.testing.assert_allclose(
                left.iloc[idx].to_numpy(dtype=float),
                right.iloc[idx].to_numpy(dtype=float),
                rtol=1e-5,
                atol=1e-8,
                err_msg=f"feature parity mismatch: {col}",
            )

    def test_snapshot_accessor_preserves_zero_atm_values(self) -> None:
        snap = SnapshotAccessor(
            {
                "chain_aggregates": {
                    "total_ce_oi": 0.0,
                    "total_pe_oi": 0.0,
                    "pcr": 0.0,
                },
                "atm_options": {
                    "atm_ce_volume": 0.0,
                    "atm_pe_volume": 0.0,
                    "atm_ce_oi": 0.0,
                    "atm_pe_oi": 0.0,
                    "atm_ce_iv": 0.0,
                    "atm_pe_iv": 0.0,
                },
                "iv_derived": {
                    "iv_skew": 0.0,
                    "iv_percentile": 0.0,
                }
            }
        )

        self.assertEqual(snap.total_ce_oi, 0.0)
        self.assertEqual(snap.total_pe_oi, 0.0)
        self.assertEqual(snap.pcr, 0.0)
        self.assertEqual(snap.atm_ce_volume, 0.0)
        self.assertEqual(snap.atm_pe_volume, 0.0)
        self.assertEqual(snap.atm_ce_oi, 0.0)
        self.assertEqual(snap.atm_pe_oi, 0.0)
        self.assertEqual(snap.atm_ce_iv, 0.0)
        self.assertEqual(snap.atm_pe_iv, 0.0)
        self.assertEqual(snap.iv_skew, 0.0)
        self.assertEqual(snap.iv_percentile, 0.0)

    def test_stream_uses_precomputed_near_atm_oi_ratio_without_raw_strikes(self) -> None:
        state = RollingFeatureState()
        state.on_session_start(pd.Timestamp("2026-03-02").date())

        snap = SnapshotAccessor(
            {
                "snapshot_id": "snap-aggregate-only",
                "session_context": {
                    "snapshot_id": "snap-aggregate-only",
                    "timestamp": "2026-03-02T09:20:00+05:30",
                    "date": "2026-03-02",
                    "session_phase": "ACTIVE",
                    "minutes_since_open": 5,
                    "day_of_week": 0,
                    "days_to_expiry": 2,
                    "is_expiry_day": False,
                },
                "futures_bar": {
                    "fut_open": 50000.0,
                    "fut_high": 50010.0,
                    "fut_low": 49990.0,
                    "fut_close": 50005.0,
                    "fut_volume": 1000.0,
                    "fut_oi": 2000.0,
                },
                "chain_aggregates": {
                    "atm_strike": 50000,
                    "pcr": 1.0,
                    "total_ce_oi": 1000.0,
                    "total_pe_oi": 900.0,
                },
                "atm_options": {
                    "atm_ce_oi": 100.0,
                    "atm_pe_oi": 300.0,
                },
                "ladder_aggregates": {
                    "near_atm_oi_ratio": 0.61,
                },
            }
        )

        features = state.update(snap)

        self.assertAlmostEqual(float(features["near_atm_oi_ratio"]), 0.61, places=6)


if __name__ == "__main__":
    unittest.main()
