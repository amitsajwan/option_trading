"""DuckDB-backed read interface for historical parquet data."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

try:
    import duckdb
except ImportError:  # pragma: no cover - exercised in runtime
    duckdb = None  # type: ignore[assignment]


class ParquetStore:
    """Single interface for all historical parquet data access."""

    def __init__(self, base_path: str | Path) -> None:
        if duckdb is None:
            raise RuntimeError("duckdb is required. Install with: pip install duckdb")

        self.base_path = Path(base_path)
        if not self.base_path.exists():
            raise FileNotFoundError(f"parquet base path not found: {self.base_path}")

        self._futures_root = self.base_path / "futures"
        self._options_root = self.base_path / "options"
        self._spot_root = self.base_path / "spot"
        self._vix_file = self.base_path / "vix" / "vix.parquet"
        self._snapshots_root = self.base_path / "snapshots"

        self.futures_glob = self._glob(self._futures_root)
        self.options_glob = self._glob(self._options_root)
        self.spot_glob = self._glob(self._spot_root)
        self.vix_path = self._vix_file.as_posix()
        self.snapshots_glob = self._glob(self._snapshots_root)

    @staticmethod
    def _glob(root: Path) -> str:
        return (root / "**" / "*.parquet").as_posix()

    @staticmethod
    def _has_parquet(root: Path) -> bool:
        if not root.exists():
            return False
        return next(root.rglob("*.parquet"), None) is not None

    @staticmethod
    def _con() -> "duckdb.DuckDBPyConnection":
        return duckdb.connect(database=":memory:")

    def _query(self, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
        con = self._con()
        try:
            if params:
                return con.execute(sql, params).df()
            return con.execute(sql).df()
        finally:
            con.close()

    @staticmethod
    def _normalize_option_fields(df: pd.DataFrame) -> pd.DataFrame:
        """Repair strike/expiry/type from symbol when parquet fields are malformed."""
        if len(df) == 0 or "symbol" not in df.columns:
            return df

        out = df.copy()
        out["symbol"] = out["symbol"].astype(str).str.strip().str.upper()

        parsed = out["symbol"].str.extract(
            r"^(?P<underlying>[A-Z]+)"
            r"(?P<dd>\d{2})"
            r"(?P<mon>[A-Z]{3})"
            r"(?P<yy>\d{2}|\d{4})"
            r"(?P<strike>\d+)"
            r"(?P<option_type>CE|PE)$"
        )
        parsed_strike = pd.to_numeric(parsed["strike"], errors="coerce")
        parsed_expiry = parsed["dd"].fillna("") + parsed["mon"].fillna("") + parsed["yy"].fillna("")

        if "strike" not in out.columns:
            out["strike"] = parsed_strike
        else:
            raw = pd.to_numeric(out["strike"], errors="coerce")
            needs_fix = raw.isna() | (raw < 10000)
            raw.loc[needs_fix] = parsed_strike.loc[needs_fix]
            out["strike"] = raw

        if "option_type" not in out.columns:
            out["option_type"] = parsed["option_type"]
        else:
            option_type = out["option_type"].astype(str).str.upper().str.strip()
            invalid = ~option_type.isin(["CE", "PE"])
            option_type.loc[invalid] = parsed["option_type"].loc[invalid]
            out["option_type"] = option_type

        if "expiry_str" not in out.columns:
            out["expiry_str"] = parsed_expiry
        else:
            # Converted parquet can carry malformed expiry_str (e.g. mixed strike digits).
            # Prefer symbol-derived expiry whenever we can parse it.
            expiry_str = out["expiry_str"].astype(str).str.strip().str.upper()
            parsed_ok = parsed_expiry.str.len() >= 7
            expiry_str.loc[parsed_ok] = parsed_expiry.loc[parsed_ok]
            out["expiry_str"] = expiry_str

        return out

    def available_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        """Return sorted futures trade dates, optionally filtered."""
        if not self._has_parquet(self._futures_root):
            return []

        where = []
        params: list[Any] = []
        if min_day:
            where.append("trade_date >= ?")
            params.append(min_day)
        if max_day:
            where.append("trade_date <= ?")
            params.append(max_day)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        df = self._query(
            f"""
            SELECT DISTINCT trade_date
            FROM read_parquet('{self.futures_glob}', hive_partitioning=true)
            {clause}
            ORDER BY trade_date ASC
            """,
            params=params,
        )
        return df["trade_date"].astype(str).tolist() if len(df) else []

    def available_snapshot_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        """Return sorted trade dates that already exist in snapshots parquet."""
        if not self._has_parquet(self._snapshots_root):
            return []

        where = []
        params: list[Any] = []
        if min_day:
            where.append("trade_date >= ?")
            params.append(min_day)
        if max_day:
            where.append("trade_date <= ?")
            params.append(max_day)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        df = self._query(
            f"""
            SELECT DISTINCT trade_date
            FROM read_parquet('{self.snapshots_glob}', hive_partitioning=true)
            {clause}
            ORDER BY trade_date ASC
            """,
            params=params,
        )
        return df["trade_date"].astype(str).tolist() if len(df) else []

    def has_options_for_day(self, trade_date: str) -> bool:
        """Fast existence check used by batch planner/runner."""
        if not self._has_parquet(self._options_root):
            return False
        df = self._query(
            f"""
            SELECT 1 AS ok
            FROM read_parquet('{self.options_glob}', hive_partitioning=true)
            WHERE trade_date = ?
            LIMIT 1
            """,
            params=[trade_date],
        )
        return len(df) > 0

    def futures_window(self, trade_date: str, lookback_days: int = 30) -> pd.DataFrame:
        """Return futures bars for day and prior lookback trading days."""
        if not self._has_parquet(self._futures_root):
            return pd.DataFrame()

        n_days = max(0, int(lookback_days)) + 1
        df = self._query(
            f"""
            WITH selected_days AS (
                SELECT trade_date
                FROM (
                    SELECT DISTINCT trade_date
                    FROM read_parquet('{self.futures_glob}', hive_partitioning=true)
                    WHERE trade_date <= ?
                    ORDER BY trade_date DESC
                    LIMIT {n_days}
                ) d
            )
            SELECT
                timestamp,
                trade_date,
                symbol,
                open, high, low, close,
                volume, oi
            FROM read_parquet('{self.futures_glob}', hive_partitioning=true)
            WHERE trade_date IN (SELECT trade_date FROM selected_days)
            ORDER BY timestamp ASC
            """,
            params=[trade_date],
        )
        if len(df):
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            for col in ("open", "high", "low", "close", "volume", "oi"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def options_for_day(self, trade_date: str) -> pd.DataFrame:
        """Return all option rows for one trade day."""
        if not self._has_parquet(self._options_root):
            return pd.DataFrame()

        df = self._query(
            f"""
            SELECT
                timestamp,
                trade_date,
                symbol,
                strike,
                option_type,
                expiry_str,
                open, high, low, close,
                volume, oi
            FROM read_parquet('{self.options_glob}', hive_partitioning=true)
            WHERE trade_date = ?
            ORDER BY timestamp ASC, symbol ASC
            """,
            params=[trade_date],
        )
        if len(df):
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            for col in ("open", "high", "low", "close", "volume", "oi"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = self._normalize_option_fields(df)
            df["strike"] = pd.to_numeric(df.get("strike"), errors="coerce")
        return df

    def vix(self) -> pd.DataFrame:
        """Return full VIX daily series (empty if file unavailable)."""
        if not self._vix_file.exists():
            return pd.DataFrame()

        df = self._query(
            f"""
            SELECT
                trade_date,
                vix_open, vix_high, vix_low, vix_close, vix_prev_close
            FROM read_parquet('{self.vix_path}')
            ORDER BY trade_date ASC
            """
        )
        if len(df):
            for col in ("vix_open", "vix_high", "vix_low", "vix_close", "vix_prev_close"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def snapshots_for_date_range(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Return flattened snapshots between dates (inclusive)."""
        if not self._has_parquet(self._snapshots_root):
            return pd.DataFrame()

        df = self._query(
            f"""
            SELECT *
            FROM read_parquet('{self.snapshots_glob}', hive_partitioning=true)
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY timestamp ASC
            """,
            params=[start_date, end_date],
        )
        if len(df) and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        return df

    def summary(self) -> dict[str, Any]:
        """Return basic dataset stats for quick diagnostics."""
        out: dict[str, Any] = {}

        datasets = [
            ("futures", self._futures_root, self.futures_glob),
            ("options", self._options_root, self.options_glob),
            ("spot", self._spot_root, self.spot_glob),
            ("snapshots", self._snapshots_root, self.snapshots_glob),
        ]
        for name, root, glob_expr in datasets:
            if not self._has_parquet(root):
                out[name] = {"status": "missing"}
                continue
            try:
                df = self._query(
                    f"""
                    SELECT
                        COUNT(*) AS rows,
                        MIN(trade_date) AS first_day,
                        MAX(trade_date) AS last_day,
                        COUNT(DISTINCT trade_date) AS trading_days
                    FROM read_parquet('{glob_expr}', hive_partitioning=true)
                    """
                )
                out[name] = df.iloc[0].to_dict() if len(df) else {"status": "empty"}
            except Exception as exc:  # pragma: no cover - defensive
                out[name] = {"status": "error", "error": str(exc)}

        if self._vix_file.exists():
            try:
                df = self._query(
                    f"""
                    SELECT
                        COUNT(*) AS rows,
                        MIN(trade_date) AS first_day,
                        MAX(trade_date) AS last_day
                    FROM read_parquet('{self.vix_path}')
                    """
                )
                out["vix"] = df.iloc[0].to_dict() if len(df) else {"status": "empty"}
            except Exception as exc:  # pragma: no cover - defensive
                out["vix"] = {"status": "error", "error": str(exc)}
        else:
            out["vix"] = {"status": "missing"}

        return out
