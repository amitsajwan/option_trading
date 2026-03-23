"""Shared snapshot input access contract for canonical and derived snapshot datasets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import duckdb
except ImportError:  # pragma: no cover - exercised in runtime
    duckdb = None  # type: ignore[assignment]


SNAPSHOT_INPUT_MODE_ML_FLAT = "ml_flat"
SNAPSHOT_INPUT_MODE_CANONICAL = "canonical"
SNAPSHOT_INPUT_MODES = frozenset({SNAPSHOT_INPUT_MODE_ML_FLAT, SNAPSHOT_INPUT_MODE_CANONICAL})

SNAPSHOT_DATASET_ML_FLAT = "snapshots_ml_flat"
SNAPSHOT_DATASET_CANONICAL = "snapshots"
DEFAULT_EXTERNAL_DATA_ROOT = Path(__file__).resolve().parents[2] / ".data" / "ml_pipeline"
DEFAULT_HISTORICAL_PARQUET_BASE = DEFAULT_EXTERNAL_DATA_ROOT / "parquet_data"


@dataclass(frozen=True)
class SnapshotAccessInfo:
    snapshot_input_mode: str
    dataset_name: str
    snapshot_source_root: str
    snapshot_min_trade_date: Optional[str]
    snapshot_max_trade_date: Optional[str]
    snapshot_trading_days: int
    requested_min_trade_date: Optional[str]
    requested_max_trade_date: Optional[str]

    @property
    def mode(self) -> str:
        return str(self.snapshot_input_mode)

    def to_metadata(self) -> dict[str, object]:
        return {
            "snapshot_input_mode": self.snapshot_input_mode,
            "snapshot_dataset_name": self.dataset_name,
            "snapshot_source_root": self.snapshot_source_root,
            "snapshot_min_trade_date": self.snapshot_min_trade_date,
            "snapshot_max_trade_date": self.snapshot_max_trade_date,
            "snapshot_trading_days": int(self.snapshot_trading_days),
            "requested_snapshot_min_trade_date": self.requested_min_trade_date,
            "requested_snapshot_max_trade_date": self.requested_max_trade_date,
        }


def _has_parquet(root: Path, *, mode: str) -> bool:
    if not root.exists():
        return False
    return next(root.rglob("*.parquet"), None) is not None


def _glob_expr(root: Path, *, mode: str) -> str:
    return (root / "**" / "data.parquet").as_posix()


def _query_trade_day_summary(glob_expr: str) -> tuple[Optional[str], Optional[str], int]:
    if duckdb is None:
        raise RuntimeError("duckdb is required. Install with: pip install duckdb")
    con = duckdb.connect(database=":memory:")
    try:
        row = con.execute(
            f"""
            SELECT
                MIN(CAST(trade_date AS VARCHAR)) AS min_trade_date,
                MAX(CAST(trade_date AS VARCHAR)) AS max_trade_date,
                COUNT(DISTINCT trade_date) AS trading_days
            FROM read_parquet('{glob_expr}', hive_partitioning=true, union_by_name=true)
            WHERE trade_date IS NOT NULL
            """
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None, None, 0
    min_day = str(row[0]).strip() if row[0] is not None else None
    max_day = str(row[1]).strip() if row[1] is not None else None
    trading_days = int(row[2] or 0)
    return min_day, max_day, trading_days


def _validate_mode(mode: str) -> str:
    text = str(mode or "").strip().lower()
    if text not in SNAPSHOT_INPUT_MODES:
        allowed = ", ".join(sorted(SNAPSHOT_INPUT_MODES))
        raise ValueError(f"unsupported snapshot input mode '{mode}'; expected one of: {allowed}")
    return text


def _raise_post_archive_error(
    *,
    context: str,
    requested_max_day: str,
    available_max_day: str,
    mode: str,
) -> None:
    if mode == SNAPSHOT_INPUT_MODE_CANONICAL:
        raise FileNotFoundError(
            f"{context} requires canonical `snapshots` parquet with `snapshot_raw_json` through {requested_max_day}, "
            f"but available canonical `snapshots` only run through {available_max_day}."
        )
    raise FileNotFoundError(
        f"{context} requires snapshots_ml_flat input through {requested_max_day}, "
        f"but available snapshots_ml_flat only run through {available_max_day}."
    )


def require_snapshot_access(
    *,
    mode: str,
    context: str,
    parquet_base: str | Path | None = None,
    snapshot_root: str | Path | None = None,
    min_day: Optional[str] = None,
    max_day: Optional[str] = None,
) -> SnapshotAccessInfo:
    resolved_mode = _validate_mode(mode)
    context_text = str(context or "snapshot_access").strip() or "snapshot_access"

    if resolved_mode == SNAPSHOT_INPUT_MODE_ML_FLAT:
        root = Path(snapshot_root) if snapshot_root is not None else None
        if root is None and parquet_base is not None:
            root = Path(parquet_base) / SNAPSHOT_DATASET_ML_FLAT
        if root is None:
            raise FileNotFoundError(
                f"{context_text} requires derived snapshots_ml_flat input. "
                "Pass snapshot_root or provide a parquet base with snapshots_ml_flat."
            )
        if not _has_parquet(root, mode=resolved_mode):
            raise FileNotFoundError(
                f"{context_text} requires derived snapshots_ml_flat input. "
                f"Expected root: {str(root).replace(chr(92), '/')}"
            )
        dataset_name = SNAPSHOT_DATASET_ML_FLAT
    else:
        if parquet_base is None:
            raise FileNotFoundError(
                f"{context_text} requires canonical `snapshots` parquet with `snapshot_raw_json`, "
                "but no parquet base was provided."
            )
        base = Path(parquet_base)
        root = base / SNAPSHOT_DATASET_CANONICAL
        if not _has_parquet(root, mode=resolved_mode):
            ml_flat_root = base / SNAPSHOT_DATASET_ML_FLAT
            if _has_parquet(ml_flat_root, mode=SNAPSHOT_INPUT_MODE_ML_FLAT):
                raise FileNotFoundError(
                    f"{context_text} requires canonical `snapshots` parquet with `snapshot_raw_json`, "
                    "but this environment only has `snapshots_ml_flat`."
                )
            raise FileNotFoundError(
                f"{context_text} requires canonical `snapshots` parquet with `snapshot_raw_json`, "
                f"but dataset was not found under {str(root).replace(chr(92), '/')}."
            )
        dataset_name = SNAPSHOT_DATASET_CANONICAL

    glob_expr = _glob_expr(root, mode=resolved_mode)
    min_trade_date, max_trade_date, trading_days = _query_trade_day_summary(glob_expr)
    if trading_days <= 0 or max_trade_date is None:
        raise FileNotFoundError(
            f"{context_text} found dataset={dataset_name}, but no snapshot trade dates were available under "
            f"{str(root).replace(chr(92), '/')}."
        )
    requested_max = str(max_day).strip() if str(max_day or "").strip() else None
    requested_min = str(min_day).strip() if str(min_day or "").strip() else None
    if requested_max is not None and requested_max > str(max_trade_date):
        _raise_post_archive_error(
            context=context_text,
            requested_max_day=requested_max,
            available_max_day=str(max_trade_date),
            mode=resolved_mode,
        )

    return SnapshotAccessInfo(
        snapshot_input_mode=resolved_mode,
        dataset_name=dataset_name,
        snapshot_source_root=str(root.resolve()).replace("\\", "/"),
        snapshot_min_trade_date=(str(min_trade_date) if min_trade_date else None),
        snapshot_max_trade_date=str(max_trade_date),
        snapshot_trading_days=int(trading_days),
        requested_min_trade_date=requested_min,
        requested_max_trade_date=requested_max,
    )
