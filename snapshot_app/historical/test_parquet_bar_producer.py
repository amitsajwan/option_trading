import unittest
from unittest import mock

from snapshot_app.historical.parquet_bar_producer import ParquetBarProducer


class ParquetBarProducerTests(unittest.TestCase):
    @mock.patch("snapshot_app.historical.parquet_bar_producer.ParquetStore")
    def test_iter_snapshots_fails_after_legacy_snapshot_removal(
        self,
        mock_store_cls: mock.Mock,
    ) -> None:
        store = mock.Mock()
        store.available_days.return_value = ["2026-03-02"]
        store.vix.return_value = mock.Mock()
        store.has_options_for_day.return_value = True
        mock_store_cls.return_value = store

        producer = ParquetBarProducer(parquet_base="ignored")
        with self.assertRaises(RuntimeError):
            list(producer.iter_snapshots())

    @mock.patch("snapshot_app.historical.parquet_bar_producer.ParquetStore")
    def test_collect_stats_fails_after_legacy_snapshot_removal(
        self,
        mock_store_cls: mock.Mock,
    ) -> None:
        store = mock.Mock()
        store.available_days.return_value = ["2026-03-02", "2026-03-03"]
        store.vix.return_value = mock.Mock()
        store.has_options_for_day.side_effect = [True, False]
        mock_store_cls.return_value = store

        producer = ParquetBarProducer(parquet_base="ignored")
        with self.assertRaises(RuntimeError):
            producer.collect_stats()


if __name__ == "__main__":
    unittest.main()
