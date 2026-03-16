"""Unsupported legacy replay adapter kept only for explicit failure messaging."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .parquet_store import ParquetStore
from .snapshot_access import SNAPSHOT_DATASET_ML_FLAT
from .snapshot_batch import OUTPUT_DATASET_ML_FLAT

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProducerStats:
    days_total: int
    days_emitted: int
    days_skipped_missing_inputs: int
    snapshots_emitted: int


def _unsupported_message() -> str:
    return (
        "ParquetBarProducer is unsupported after legacy snapshot removal. "
        f"Historical builds now persist only `{OUTPUT_DATASET_ML_FLAT}`, which does not include `snapshot_raw_json`."
    )


class ParquetBarProducer:
    """Legacy replay adapter kept only to fail explicitly on the v1-only lane."""

    def __init__(
        self,
        *,
        parquet_base: str | Path,
        instrument: str = "BANKNIFTY-I",
        min_day: Optional[str] = None,
        max_day: Optional[str] = None,
        explicit_days: Optional[list[str]] = None,
        lookback_days: int = 30,
    ) -> None:
        self._store = ParquetStore(parquet_base, snapshots_dataset=SNAPSHOT_DATASET_ML_FLAT)
        self._instrument = str(instrument).strip() or "BANKNIFTY-I"
        self._lookback_days = max(0, int(lookback_days))
        all_days = self._store.available_days(min_day=min_day, max_day=max_day)
        if explicit_days:
            wanted = {str(day).strip() for day in explicit_days if str(day).strip()}
            all_days = [day for day in all_days if day in wanted]
        self._days = [str(day) for day in all_days]
        self._vix_daily = self._store.vix()

    @property
    def days(self) -> list[str]:
        return list(self._days)

    def iter_snapshots(self) -> Iterator[dict]:
        raise RuntimeError(_unsupported_message())
        yield {}

    def collect_stats(self) -> ProducerStats:
        raise RuntimeError(_unsupported_message())
