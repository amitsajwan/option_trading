import unittest

import numpy as np
import pandas as pd

from snapshot_app.runtime_features import _add_group_features
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
                "pcr": 1.0,
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
                "ce_oi_total": 1_200_000.0 + 300.0 * np.arange(n),
                "pe_oi_total": 1_100_000.0 + 240.0 * np.arange(n),
                "ce_volume_total": 250000.0 + 500.0 * np.arange(n),
                "pe_volume_total": 220000.0 + 450.0 * np.arange(n),
                "opt_0_ce_iv": np.full(n, 0.16),
                "opt_0_pe_iv": np.full(n, 0.17),
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


if __name__ == "__main__":
    unittest.main()
