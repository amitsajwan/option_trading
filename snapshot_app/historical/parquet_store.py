"""DuckDB-backed read interface for historical parquet data."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    import duckdb
except ImportError:  # pragma: no cover - exercised in runtime
    duckdb = None  # type: ignore[assignment]


class ParquetStore:
    """Single interface for all historical parquet data access."""

    def __init__(self, base_path: str | Path, *, snapshots_dataset: str = "snapshots") -> None:
        if duckdb is None:
            raise RuntimeError("duckdb is required. Install with: pip install duckdb")

        self.base_path = Path(base_path)
        if not self.base_path.exists():
            raise FileNotFoundError(f"parquet base path not found: {self.base_path}")

        self._futures_root = self.base_path / "futures"
        self._options_root = self.base_path / "options"
        self._spot_root = self.base_path / "spot"
        self._vix_file = self.base_path / "vix" / "vix.parquet"
        dataset = str(snapshots_dataset or "snapshots").strip() or "snapshots"
        self._snapshots_dataset = dataset
        self._snapshots_root = self.base_path / dataset

        self.futures_glob = self._glob(self._futures_root)
        self.options_glob = self._glob(self._options_root)
        self.spot_glob = self._glob(self._spot_root)
        self.vix_path = self._vix_file.as_posix()
        # Restrict snapshots to canonical yearly files only.
        # This avoids accidental reads of quarantined/corrupt backups.
        self.snapshots_glob = (self._snapshots_root / "**" / "data.parquet").as_posix()
        self._options_columns: Optional[frozenset[str]] = None

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
            return con.execute(sql, params or None).df()
        finally:
            con.close()

    def _ensure_snapshots_available(self, *, context: str) -> bool:
        if self._has_parquet(self._snapshots_root):
            return True
        if self._snapshots_dataset == "snapshots":
            ml_flat_root = self.base_path / "snapshots_ml_flat"
            if self._has_parquet(ml_flat_root):
                raise FileNotFoundError(
                    f"{context} requires canonical `snapshots` parquet with `snapshot_raw_json`, "
                    "but this environment only has `snapshots_ml_flat`."
                )
        return False

    def _probe_options_columns(self) -> frozenset[str]:
        """Return cached option parquet column names."""
        if self._options_columns is not None:
            return self._options_columns
        if not self._has_parquet(self._options_root):
            self._options_columns = frozenset()
            return self._options_columns
        try:
            schema = self._query(
                f"""
                SELECT *
                FROM read_parquet('{self.options_glob}', hive_partitioning=true)
                LIMIT 0
                """
            )
            self._options_columns = frozenset(schema.columns)
        except Exception:
            self._options_columns = frozenset()
        return self._options_columns

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
            params=params or None,
        )
        return df["trade_date"].astype(str).tolist() if len(df) else []

    def available_snapshot_days(self, min_day: str | None = None, max_day: str | None = None) -> list[str]:
        """Return sorted trade dates that already exist in snapshots parquet."""
        if not self._ensure_snapshots_available(context="available_snapshot_days"):
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
            FROM read_parquet('{self.snapshots_glob}', hive_partitioning=true, union_by_name=true)
            {clause}
            ORDER BY trade_date ASC
            """,
            params=params or None,
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

        opt_cols = self._probe_options_columns()
        iv_expr = "iv" if "iv" in opt_cols else "NULL::DOUBLE AS iv"
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
                volume, oi,
                {iv_expr}
            FROM read_parquet('{self.options_glob}', hive_partitioning=true)
            WHERE trade_date = ?
            ORDER BY timestamp ASC, symbol ASC
            """,
            params=[trade_date],
        )
        if len(df):
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            for col in ("open", "high", "low", "close", "volume", "oi", "iv"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = self._normalize_option_fields(df)
            df["strike"] = pd.to_numeric(df.get("strike"), errors="coerce")
        return df

    def spot_for_day(self, trade_date: str) -> pd.DataFrame:
        """Return spot rows for one trade day."""
        if not self._has_parquet(self._spot_root):
            return pd.DataFrame()

        df = self._query(
            f"""
            SELECT
                timestamp,
                trade_date,
                symbol,
                open, high, low, close
            FROM read_parquet('{self.spot_glob}', hive_partitioning=true)
            WHERE trade_date = ?
            ORDER BY timestamp ASC
            """,
            params=[trade_date],
        )
        if len(df):
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            for col in ("open", "high", "low", "close"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
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
        if not self._ensure_snapshots_available(context="snapshots_for_date_range"):
            return pd.DataFrame()

        df = self._query(
            f"""
            SELECT *
            FROM read_parquet('{self.snapshots_glob}', hive_partitioning=true, union_by_name=true)
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY timestamp ASC
            """,
            params=[start_date, end_date],
        )
        if len(df) and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        return df

    def snapshot_field_coverage(
        self,
        fields: list[str],
        min_day: str | None = None,
        max_day: str | None = None,
    ) -> pd.DataFrame:
        """Return per-day non-null counts for selected snapshot fields."""
        if not self._ensure_snapshots_available(context="snapshot_field_coverage"):
            return pd.DataFrame()

        fields = [str(field).strip() for field in fields if str(field).strip()]
        if not fields:
            return pd.DataFrame()

        schema = self._query(
            f"""
            SELECT *
            FROM read_parquet('{self.snapshots_glob}', hive_partitioning=true, union_by_name=true)
            LIMIT 0
            """
        )
        available = set(schema.columns)
        present_fields = [field for field in fields if field in available]
        missing_fields = [field for field in fields if field not in available]

        projections = ["trade_date", "COUNT(*) AS row_count"]
        projections.extend(
            f"SUM(CASE WHEN {field} IS NOT NULL THEN 1 ELSE 0 END) AS nn__{field}" for field in present_fields
        )
        projections.extend(f"CAST(0 AS BIGINT) AS nn__{field}" for field in missing_fields)

        where = []
        params: list[Any] = []
        if min_day:
            where.append("trade_date >= ?")
            params.append(min_day)
        if max_day:
            where.append("trade_date <= ?")
            params.append(max_day)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        return self._query(
            f"""
            SELECT
                {", ".join(projections)}
            FROM read_parquet('{self.snapshots_glob}', hive_partitioning=true, union_by_name=true)
            {clause}
            GROUP BY trade_date
            ORDER BY trade_date ASC
            """,
            params=params or None,
        )

    def snapshot_schema_version_coverage(
        self,
        *,
        min_day: str | None = None,
        max_day: str | None = None,
    ) -> pd.DataFrame:
        """Return per-day schema_version coverage for snapshots parquet."""
        if not self._ensure_snapshots_available(context="snapshot_schema_version_coverage"):
            return pd.DataFrame()

        schema = self._query(
            f"""
            SELECT *
            FROM read_parquet('{self.snapshots_glob}', hive_partitioning=true, union_by_name=true)
            LIMIT 0
            """
        )
        if "schema_version" not in set(schema.columns):
            return pd.DataFrame(
                columns=["trade_date", "row_count", "rows_with_schema_version", "min_schema_version", "max_schema_version"]
            )

        where = []
        params: list[Any] = []
        if min_day:
            where.append("trade_date >= ?")
            params.append(min_day)
        if max_day:
            where.append("trade_date <= ?")
            params.append(max_day)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        return self._query(
            f"""
            SELECT
                trade_date,
                COUNT(*) AS row_count,
                SUM(CASE WHEN schema_version IS NOT NULL THEN 1 ELSE 0 END) AS rows_with_schema_version,
                MIN(CAST(schema_version AS VARCHAR)) AS min_schema_version,
                MAX(CAST(schema_version AS VARCHAR)) AS max_schema_version
            FROM read_parquet('{self.snapshots_glob}', hive_partitioning=true, union_by_name=true)
            {clause}
            GROUP BY trade_date
            ORDER BY trade_date ASC
            """,
            params=params or None,
        )

    @staticmethod
    def _latest_contiguous_block_from_coverage(
        coverage: pd.DataFrame,
        *,
        required_schema_version: str,
        max_gap_days: int = 7,
    ) -> Optional[dict[str, Any]]:
        """Compute latest contiguous block of required schema days from coverage rows."""
        if coverage is None or len(coverage) == 0:
            return None

        work = coverage.copy()
        work["trade_date"] = pd.to_datetime(work.get("trade_date"), errors="coerce")
        work = work.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
        if len(work) == 0:
            return None

        required = str(required_schema_version).strip()
        work["min_schema_version"] = work.get("min_schema_version").astype(str).str.strip()
        work["max_schema_version"] = work.get("max_schema_version").astype(str).str.strip()
        work["row_count"] = pd.to_numeric(work.get("row_count"), errors="coerce").fillna(0).astype(int)
        work["rows_with_schema_version"] = (
            pd.to_numeric(work.get("rows_with_schema_version"), errors="coerce").fillna(0).astype(int)
        )
        work["is_required_schema_day"] = (
            (work["row_count"] > 0)
            & (work["rows_with_schema_version"] == work["row_count"])
            & (work["min_schema_version"] == required)
            & (work["max_schema_version"] == required)
        )

        rows = work.to_dict(orient="records")
        block: list[dict[str, Any]] = []
        latest_kept: Optional[date] = None
        gap_limit = max(1, int(max_gap_days))

        for row in reversed(rows):
            if not bool(row.get("is_required_schema_day")):
                if block:
                    break
                continue
            trade_day = pd.Timestamp(row["trade_date"]).date()
            if latest_kept is None:
                block.append(row)
                latest_kept = trade_day
                continue
            gap_days = (latest_kept - trade_day).days
            if gap_days <= gap_limit:
                block.append(row)
                latest_kept = trade_day
                continue
            break

        if not block:
            return None

        block = list(reversed(block))
        days = [pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d") for row in block]
        return {
            "window_start": days[0],
            "window_end": days[-1],
            "trading_days": int(len(days)),
            "all_days_required_schema": bool(all(bool(row.get("is_required_schema_day")) for row in block)),
            "schema_version": required,
            "days": days,
            "max_gap_days": gap_limit,
        }

    def latest_contiguous_snapshot_block(
        self,
        *,
        required_schema_version: str = "3.0",
        min_day: str | None = None,
        max_day: str | None = None,
        max_gap_days: int = 7,
    ) -> Optional[dict[str, Any]]:
        """Return latest contiguous required-schema block over snapshot days."""
        coverage = self.snapshot_schema_version_coverage(min_day=min_day, max_day=max_day)
        return self._latest_contiguous_block_from_coverage(
            coverage=coverage,
            required_schema_version=required_schema_version,
            max_gap_days=max_gap_days,
        )

    def build_window_readiness_artifact(
        self,
        *,
        required_schema_version: str = "3.0",
        min_trading_days: int = 150,
        min_day: str | None = None,
        max_day: str | None = None,
        max_gap_days: int = 7,
    ) -> dict[str, Any]:
        """Build canonical latest-window readiness artifact."""
        block = self.latest_contiguous_snapshot_block(
            required_schema_version=required_schema_version,
            min_day=min_day,
            max_day=max_day,
            max_gap_days=max_gap_days,
        )
        trading_days = int((block or {}).get("trading_days") or 0)
        all_days_required_schema = bool((block or {}).get("all_days_required_schema"))
        formal_ready = bool(
            all_days_required_schema
            and str((block or {}).get("schema_version") or "").strip() == str(required_schema_version).strip()
            and trading_days >= int(min_trading_days)
        )
        artifact = {
            "window_start": (block or {}).get("window_start"),
            "window_end": (block or {}).get("window_end"),
            "trading_days": trading_days,
            "all_days_required_schema": all_days_required_schema,
            "schema_version": str(required_schema_version),
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_path": str(self._snapshots_root.resolve()),
            "formal_ready": formal_ready,
            "min_trading_days_required": int(min_trading_days),
            "max_gap_days": int(max_gap_days),
            "exploratory_only": (not formal_ready),
        }
        return artifact

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
        out["options_has_iv"] = "iv" in self._probe_options_columns()

        return out
