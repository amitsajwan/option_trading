from snapshot_app.historical.replay_runner import DEFAULT_PARQUET_BASE as REPLAY_DEFAULT_PARQUET_BASE
from snapshot_app.historical.snapshot_access import DEFAULT_HISTORICAL_PARQUET_BASE
from snapshot_app.historical.snapshot_batch import _load_ml_flat_required_columns
from snapshot_app.historical.snapshot_batch_runner import DEFAULT_PARQUET_BASE as BATCH_DEFAULT_PARQUET_BASE
from snapshot_app.snapshot_ml_flat_contract import load_contract_schema


def test_historical_runners_share_default_parquet_root() -> None:
    assert BATCH_DEFAULT_PARQUET_BASE == DEFAULT_HISTORICAL_PARQUET_BASE
    assert REPLAY_DEFAULT_PARQUET_BASE == DEFAULT_HISTORICAL_PARQUET_BASE


def test_historical_batch_uses_snapshot_app_contract_columns() -> None:
    assert _load_ml_flat_required_columns() == load_contract_schema()["required_columns"]
