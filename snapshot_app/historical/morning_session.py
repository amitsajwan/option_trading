"""
MorningSessionLoader — loads all snapshots from 10:00 to 11:30 for a trade_date.

Primary source:  snapshots_ml_flat parquet (has all intraday rows with OI, PCR, price, vol).
IV enrichment:   raw snapshots parquet (has snapshot_raw_json with atm_ce_iv / atm_pe_iv).
                 If raw snapshots unavailable, IV columns are left NaN.

Output:  DataFrame sorted ascending by timestamp, one row per 15-min snapshot.
         Columns are the full ml_flat schema PLUS atm_ce_iv, atm_pe_iv, iv_skew (from raw JSON).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── time window: 10:00 to 11:30 IST ───────────────────────────────────────────
_WINDOW_START_HOUR = 10
_WINDOW_START_MIN = 0
_WINDOW_END_HOUR = 11
_WINDOW_END_MIN = 30

# ── column names ───────────────────────────────────────────────────────────────
_ATM_CE_IV = "atm_ce_iv"
_ATM_PE_IV = "atm_pe_iv"
_IV_SKEW = "iv_skew"


def _is_in_morning_window(ts: pd.Timestamp) -> bool:
    hour, minute = ts.hour, ts.minute
    after_start = (hour > _WINDOW_START_HOUR) or (
        hour == _WINDOW_START_HOUR and minute >= _WINDOW_START_MIN
    )
    before_end = (hour < _WINDOW_END_HOUR) or (
        hour == _WINDOW_END_HOUR and minute <= _WINDOW_END_MIN
    )
    return after_start and before_end


def _filter_morning_window(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only rows whose timestamp falls in [10:00, 11:30]."""
    if len(df) == 0:
        return df
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    mask = ts.apply(lambda t: _is_in_morning_window(t) if pd.notna(t) else False)
    return df[mask].copy()


def _extract_iv_from_raw_json(raw_json: str) -> Dict[str, Any]:
    """
    Parse one snapshot_raw_json string and extract ATM IV values.

    The raw JSON structure is the full MarketSnapshot dict.
    atm_ce_iv lives at: snapshot["atm_options"]["atm_ce_iv"]
    atm_pe_iv lives at: snapshot["atm_options"]["atm_pe_iv"]
    iv_skew   lives at: snapshot["iv_derived"]["iv_skew"]
    """
    result: Dict[str, Any] = {
        _ATM_CE_IV: None,
        _ATM_PE_IV: None,
        _IV_SKEW: None,
    }
    try:
        snap = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return result

    atm_opts = snap.get("atm_options")
    if isinstance(atm_opts, dict):
        result[_ATM_CE_IV] = atm_opts.get("atm_ce_iv")
        result[_ATM_PE_IV] = atm_opts.get("atm_pe_iv")

    iv_derived = snap.get("iv_derived")
    if isinstance(iv_derived, dict):
        result[_IV_SKEW] = iv_derived.get("iv_skew")

    return result


def _load_ml_flat_for_date(trade_date: str, ml_flat_root: Path) -> pd.DataFrame:
    """
    Load all snapshots_ml_flat rows for trade_date using DuckDB.
    Returns empty DataFrame on any error.
    """
    try:
        import duckdb
    except ImportError:
        logger.error("duckdb not installed — cannot load ml_flat parquet")
        return pd.DataFrame()

    glob_pattern = (ml_flat_root / "**" / "*.parquet").as_posix()
    try:
        con = duckdb.connect(":memory:")
        df: pd.DataFrame = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{glob_pattern}', hive_partitioning=false, union_by_name=true)
            WHERE trade_date = ?
            ORDER BY timestamp ASC
            """,
            [trade_date],
        ).df()
        con.close()
    except Exception as exc:
        logger.warning("ml_flat load failed for %s: %s", trade_date, exc)
        return pd.DataFrame()

    if len(df) == 0:
        return df

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


def _load_raw_snapshots_for_date(trade_date: str, raw_root: Path) -> pd.DataFrame:
    """
    Load raw snapshot_raw_json rows for trade_date.
    Returns empty DataFrame when raw snapshots unavailable.
    """
    try:
        import duckdb
    except ImportError:
        return pd.DataFrame()

    glob_pattern = (raw_root / "**" / "*.parquet").as_posix()
    try:
        con = duckdb.connect(":memory:")
        # Check columns first — raw snapshots must have snapshot_raw_json
        schema_df: pd.DataFrame = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{glob_pattern}', hive_partitioning=false, union_by_name=true)
            LIMIT 0
            """
        ).df()
        if "snapshot_raw_json" not in schema_df.columns:
            con.close()
            return pd.DataFrame()

        df: pd.DataFrame = con.execute(
            f"""
            SELECT timestamp, snapshot_raw_json
            FROM read_parquet('{glob_pattern}', hive_partitioning=false, union_by_name=true)
            WHERE trade_date = ?
            ORDER BY timestamp ASC
            """,
            [trade_date],
        ).df()
        con.close()
    except Exception as exc:
        logger.debug("raw snapshot load failed for %s: %s", trade_date, exc)
        return pd.DataFrame()

    if len(df) == 0:
        return df

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


def _enrich_with_iv(ml_flat_df: pd.DataFrame, raw_df: pd.DataFrame) -> pd.DataFrame:
    """
    Join IV values from raw snapshots onto the ml_flat DataFrame.
    Matching key: timestamp (floored to minute).

    If raw_df is empty, adds NaN IV columns and returns.
    """
    # add NaN IV columns as default
    ml_flat_df = ml_flat_df.copy()
    ml_flat_df[_ATM_CE_IV] = float("nan")
    ml_flat_df[_ATM_PE_IV] = float("nan")
    ml_flat_df[_IV_SKEW] = float("nan")

    if len(raw_df) == 0 or "snapshot_raw_json" not in raw_df.columns:
        return ml_flat_df

    # parse IV from each raw JSON row
    iv_records = []
    for _, row in raw_df.iterrows():
        ts = row.get("timestamp")
        raw_json = row.get("snapshot_raw_json")
        if pd.isna(ts) or not isinstance(raw_json, str):
            continue
        iv_vals = _extract_iv_from_raw_json(raw_json)
        # Preserve local wall-clock time for matching against ml_flat timestamps.
        ts_norm = pd.Timestamp(ts)
        if ts_norm.tzinfo is not None:
            ts_norm = ts_norm.tz_localize(None)
        iv_records.append({"_ts_key": ts_norm.floor("min"), **iv_vals})

    if not iv_records:
        return ml_flat_df

    iv_df = pd.DataFrame(iv_records).dropna(subset=["_ts_key"])
    iv_df = iv_df.drop_duplicates(subset=["_ts_key"], keep="last")

    # join onto ml_flat by floored local timestamp (timezone-naive)
    ml_flat_ts = pd.to_datetime(ml_flat_df["timestamp"], errors="coerce", utc=False)
    if ml_flat_ts.dt.tz is not None:
        ml_flat_ts = ml_flat_ts.dt.tz_localize(None)
    ml_flat_df["_ts_key"] = ml_flat_ts.dt.floor("min")
    merged = ml_flat_df.merge(
        iv_df.rename(columns={
            _ATM_CE_IV: "_iv_ce",
            _ATM_PE_IV: "_iv_pe",
            _IV_SKEW: "_iv_skew",
        }),
        on="_ts_key",
        how="left",
    )
    # fill IV columns from the joined values where not already present
    for col_out, col_in in [(_ATM_CE_IV, "_iv_ce"), (_ATM_PE_IV, "_iv_pe"), (_IV_SKEW, "_iv_skew")]:
        if col_in in merged.columns:
            filled = pd.to_numeric(merged[col_in], errors="coerce")
            merged[col_out] = filled

    # drop temp columns
    drop_cols = ["_ts_key", "_iv_ce", "_iv_pe", "_iv_skew"]
    merged = merged.drop(columns=[c for c in drop_cols if c in merged.columns])
    return merged


class MorningSessionLoader:
    """
    Loads all snapshots from 10:00 to 11:30 for a given trade_date.

    Args:
        parquet_root:  Base path of the parquet store (contains
                       snapshots_ml_flat/ and snapshots/ subdirectories).
        ml_flat_dataset:  Name of the ml_flat subdirectory (default: snapshots_ml_flat).
        raw_dataset:      Name of the raw snapshots subdirectory (default: snapshots).
                          Set to None to skip IV enrichment entirely.
    """

    def __init__(
        self,
        parquet_root: str | Path,
        *,
        ml_flat_dataset: str = "snapshots_ml_flat",
        raw_dataset: Optional[str] = "snapshots",
    ) -> None:
        self.parquet_root = Path(parquet_root)
        self.ml_flat_root = self.parquet_root / ml_flat_dataset
        self.raw_root = self.parquet_root / raw_dataset if raw_dataset else None

    def load(self, trade_date: str) -> pd.DataFrame:
        """
        Return sorted DataFrame of morning session snapshots for trade_date.

        Columns:  all ml_flat columns + atm_ce_iv, atm_pe_iv, iv_skew.
        Returns:  empty DataFrame when the date has no snapshots or the parquet
                  is unavailable.  Never raises.
        """
        if not self.ml_flat_root.exists():
            logger.warning("ml_flat root not found: %s", self.ml_flat_root)
            return pd.DataFrame()

        ml_df = _load_ml_flat_for_date(trade_date, self.ml_flat_root)
        if len(ml_df) == 0:
            return pd.DataFrame()

        morning_df = _filter_morning_window(ml_df)
        if len(morning_df) == 0:
            logger.debug("no morning window rows for %s", trade_date)
            return pd.DataFrame()

        # IV enrichment from raw snapshots
        if self.raw_root is not None and self.raw_root.exists():
            raw_df = _load_raw_snapshots_for_date(trade_date, self.raw_root)
            raw_morning = _filter_morning_window(raw_df) if len(raw_df) > 0 else pd.DataFrame()
            morning_df = _enrich_with_iv(morning_df, raw_morning)
        else:
            # add NaN IV columns so downstream code has consistent schema
            morning_df = morning_df.copy()
            morning_df["atm_ce_iv"] = float("nan")
            morning_df["atm_pe_iv"] = float("nan")
            morning_df["iv_skew"] = float("nan")

        return morning_df.sort_values("timestamp").reset_index(drop=True)

    def load_range(
        self,
        start_date: str,
        end_date: str,
    ) -> Dict[str, pd.DataFrame]:
        """
        Return {trade_date: morning_df} for all dates in [start_date, end_date].
        Dates with no data return empty DataFrames (included in the dict).
        """
        if not self.ml_flat_root.exists():
            logger.warning("ml_flat root not found: %s", self.ml_flat_root)
            return {}

        try:
            import duckdb
        except ImportError:
            logger.error("duckdb not installed")
            return {}

        # get all distinct trade_dates in range that exist in ml_flat
        glob_pattern = (self.ml_flat_root / "**" / "*.parquet").as_posix()
        try:
            con = duckdb.connect(":memory:")
            dates_df: pd.DataFrame = con.execute(
                f"""
                SELECT DISTINCT trade_date
                FROM read_parquet('{glob_pattern}', hive_partitioning=false, union_by_name=true)
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date ASC
                """,
                [start_date, end_date],
            ).df()
            con.close()
        except Exception as exc:
            logger.error("failed to enumerate dates %s–%s: %s", start_date, end_date, exc)
            return {}

        result: Dict[str, pd.DataFrame] = {}
        dates = dates_df["trade_date"].astype(str).tolist()
        for trade_date in dates:
            result[trade_date] = self.load(trade_date)

        return result


__all__ = ["MorningSessionLoader"]
