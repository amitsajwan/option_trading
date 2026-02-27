import unittest

import pandas as pd

from ml_pipeline.execution_simulator_v2 import (
    ExecutionSimulatorConfig,
    ParquetSnapshotSource,
    run_execution_simulation,
)
from ml_pipeline.fill_model import FillModelConfig


def _labeled_frame() -> pd.DataFrame:
    ts = pd.date_range("2024-10-10 10:00:00", periods=3, freq="min")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "opt_0_ce_close": [100.0, 110.0, 108.0],
            "opt_0_ce_high": [101.0, 111.0, 109.0],
            "opt_0_ce_low": [99.0, 109.0, 107.0],
            "opt_0_ce_volume": [1000.0, 1000.0, 1000.0],
            "opt_0_pe_close": [90.0, 88.0, 87.0],
            "opt_0_pe_high": [91.0, 89.0, 88.0],
            "opt_0_pe_low": [89.0, 87.0, 86.0],
            "opt_0_pe_volume": [1000.0, 1000.0, 1000.0],
            "depth_total_bid_qty": [500.0, 500.0, 500.0],
            "depth_total_ask_qty": [500.0, 500.0, 500.0],
        }
    )


class ExecutionSimulatorV2Tests(unittest.TestCase):
    def test_roundtrip_fill_and_return(self) -> None:
        events = pd.DataFrame(
            [
                {
                    "timestamp": "2024-10-10T10:00:00",
                    "event_type": "ENTRY",
                    "action": "BUY_CE",
                    "position": {"side": "CE", "entry_timestamp": "2024-10-10T10:00:00", "entry_confidence": 0.7},
                },
                {
                    "timestamp": "2024-10-10T10:01:00",
                    "event_type": "EXIT",
                    "action": "HOLD",
                    "event_reason": "time_stop",
                    "position": {"side": "CE", "entry_timestamp": "2024-10-10T10:00:00", "entry_confidence": 0.7},
                },
            ]
        )
        source = ParquetSnapshotSource(_labeled_frame())
        sim_cfg = ExecutionSimulatorConfig(
            order_latency_ms=0,
            exchange_latency_ms=0,
            max_participation_rate=1.0,
            fee_per_fill_return=0.001,
            default_order_qty=1.0,
            force_liquidate_end=True,
        )
        fill_cfg = FillModelConfig(model="constant", constant_slippage=0.0)
        exec_df, report = run_execution_simulation(
            events_df=events,
            snapshot_source=source,
            sim_cfg=sim_cfg,
            fill_cfg=fill_cfg,
        )
        self.assertEqual(report["closed_trades"], 1)
        self.assertEqual(report["rejects_total"], 0)
        self.assertEqual(report["fills_total"], 2)
        self.assertEqual(report["open_position_end_qty"], 0.0)
        self.assertGreater(len(exec_df), 0)
        self.assertAlmostEqual(report["net_return_sum"], 0.098, places=6)

    def test_partial_fill_and_forced_liquidation(self) -> None:
        frame = _labeled_frame().copy()
        frame["opt_0_ce_volume"] = [0.4, 0.4, 0.4]
        frame["depth_total_ask_qty"] = [0.4, 0.4, 0.4]
        frame["depth_total_bid_qty"] = [0.4, 0.4, 0.4]
        events = pd.DataFrame(
            [
                {
                    "timestamp": "2024-10-10T10:00:00",
                    "event_type": "ENTRY",
                    "action": "BUY_CE",
                    "position": {"side": "CE", "entry_timestamp": "2024-10-10T10:00:00", "entry_confidence": 0.7},
                },
            ]
        )
        source = ParquetSnapshotSource(frame)
        sim_cfg = ExecutionSimulatorConfig(
            order_latency_ms=0,
            exchange_latency_ms=0,
            max_participation_rate=1.0,
            fee_per_fill_return=0.0,
            default_order_qty=1.0,
            force_liquidate_end=True,
        )
        fill_cfg = FillModelConfig(model="constant", constant_slippage=0.0)
        _exec_df, report = run_execution_simulation(
            events_df=events,
            snapshot_source=source,
            sim_cfg=sim_cfg,
            fill_cfg=fill_cfg,
        )
        self.assertEqual(report["closed_trades"], 1)
        self.assertGreaterEqual(report["partial_fills_total"], 1)
        self.assertEqual(report["open_position_end_qty"], 0.0)

    def test_exit_without_open_position_rejected(self) -> None:
        events = pd.DataFrame(
            [
                {
                    "timestamp": "2024-10-10T10:00:00",
                    "event_type": "EXIT",
                    "action": "HOLD",
                    "position": {"side": "CE", "entry_timestamp": "2024-10-10T09:59:00", "entry_confidence": 0.7},
                }
            ]
        )
        source = ParquetSnapshotSource(_labeled_frame())
        sim_cfg = ExecutionSimulatorConfig(
            order_latency_ms=0,
            exchange_latency_ms=0,
            max_participation_rate=1.0,
            default_order_qty=1.0,
        )
        fill_cfg = FillModelConfig(model="constant", constant_slippage=0.0)
        _exec_df, report = run_execution_simulation(
            events_df=events,
            snapshot_source=source,
            sim_cfg=sim_cfg,
            fill_cfg=fill_cfg,
        )
        self.assertEqual(report["closed_trades"], 0)
        self.assertEqual(report["rejects_total"], 1)


if __name__ == "__main__":
    unittest.main()
