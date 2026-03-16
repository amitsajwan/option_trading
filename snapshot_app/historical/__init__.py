"""Historical snapshot build package for snapshot_app."""

from .parquet_store import ParquetStore
from .parquet_bar_producer import ParquetBarProducer


def run_snapshot_batch(*args, **kwargs):
    # Lazy import avoids pulling snapshot_batch dependencies when only ParquetStore is needed.
    from .snapshot_batch import run_snapshot_batch as _run_snapshot_batch

    return _run_snapshot_batch(*args, **kwargs)

__all__ = ["ParquetStore", "ParquetBarProducer", "run_snapshot_batch"]
