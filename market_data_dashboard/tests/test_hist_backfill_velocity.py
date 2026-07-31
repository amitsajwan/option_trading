"""_inject_velocity: historical backfill must run the same per-bar velocity
accumulator live serving does.

Regression test for the 2026-07-31 finding: backfilled snapshots
(phase1_market_snapshots_hist{,_*}) never carried snapshot["velocity_enrichment"]
at all -- confirmed against a live snapshot from the same morning, which had
it populated from ~9:18 onward. ~19 of a 47-feature entry-model bundle are
vel_*, so any model trained or backtested against this historical data was
silently getting ~40% real features, median-imputed for the rest.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from market_data_dashboard.services.hist_backfill import _inject_velocity


class InjectVelocityTest(unittest.TestCase):
    def test_sorts_and_processes_each_snapshot_in_order(self) -> None:
        snapshots = [
            {"snapshot_id": "20260701_0920", "timestamp": "2026-07-01T09:20:00+05:30"},
            {"snapshot_id": "20260701_0916", "timestamp": "2026-07-01T09:16:00+05:30"},
            {"snapshot_id": "20260701_0918", "timestamp": "2026-07-01T09:18:00+05:30"},
        ]
        seen_order = []

        def _fake_process(snap):
            seen_order.append(snap["snapshot_id"])
            return {**snap, "velocity_enrichment": {"vel_price_delta_open": 0.0}}

        fake_acc = MagicMock()
        fake_acc.process.side_effect = _fake_process

        with patch(
            "market_data_dashboard.services.hist_backfill.LiveVelocityAccumulator",
            return_value=fake_acc,
        ), patch(
            "market_data_dashboard.services.hist_backfill.make_mongo_context_provider",
            return_value=lambda trade_date: (None, None, None),
        ):
            _inject_velocity(MagicMock(), snapshots, "BANKNIFTY")

        # processed strictly in timestamp order, not input order
        self.assertEqual(seen_order, ["20260701_0916", "20260701_0918", "20260701_0920"])
        # in-place mutation: every snapshot now carries the injected key
        self.assertTrue(all("velocity_enrichment" in s for s in snapshots))

    def test_uses_the_backfill_collection_for_context_not_live(self) -> None:
        """Prior-day context (prev_day_close etc.) must come from the SAME
        historical collection being backfilled, not the live-only default
        (phase1_market_snapshots) -- otherwise every re-backfilled day would
        silently get no context for arbitrary past dates."""
        mongo_db = MagicMock()
        with patch(
            "market_data_dashboard.services.hist_backfill.LiveVelocityAccumulator"
        ) as mock_acc_cls, patch(
            "market_data_dashboard.services.hist_backfill.make_mongo_context_provider"
        ) as mock_provider:
            mock_acc_cls.return_value.process.side_effect = lambda s: s
            _inject_velocity(mongo_db, [{"timestamp": "2026-07-01T09:16:00+05:30"}], "NIFTY")

        mock_provider.assert_called_once_with(
            mongo_db, collections=("phase1_market_snapshots_hist_nifty",)
        )


if __name__ == "__main__":
    unittest.main()
