"""Canonical entry-candidate dataset builder for entry-quality ML."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.snapshot_access import (
    SNAPSHOT_DATASET_CANONICAL,
    SNAPSHOT_INPUT_MODE_CANONICAL,
    require_snapshot_access,
)
from snapshot_app.historical.window_manifest import (
    DEFAULT_MIN_TRADING_DAYS,
    DEFAULT_REQUIRED_SCHEMA_VERSION,
    load_and_validate_window_manifest,
    split_boundaries_for_days,
)
from strategy_app.contracts import Direction, PositionContext, StrategyVote, TradeSignal
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.engines.snapshot_accessor import SnapshotAccessor

from .entry_quality_config import (
    DEFAULT_CANDIDATE_ROOT,
    DEFAULT_CONTINUOUS_END,
    DEFAULT_CONTINUOUS_START,
    EntryQualityDatasetConfig,
    EntryQualitySplitConfig,
    LABEL_PROFILES,
    split_name_for_trade_date,
)
from .snapshot_quality_gate import REQUIRED_SNAPSHOT_SCHEMA_VERSION, enforce_snapshot_schema_version


@dataclass(frozen=True)
class CandidateRow:
    snapshot_id: str
    trade_date: str
    timestamp: str
    strategy_name: str
    direction: str
    vote_confidence: float
    vote_reason: str
    proposed_strike: Optional[int]
    proposed_entry_premium: Optional[float]
    regime: Optional[str]
    regime_confidence: Optional[float]
    regime_reason: Optional[str]
    policy_allowed: Optional[bool]
    policy_score: Optional[float]
    policy_reason: Optional[str]
    policy_check_volume: Optional[str]
    policy_check_momentum: Optional[str]
    policy_check_timing: Optional[str]
    policy_check_premium: Optional[str]
    policy_check_regime: Optional[str]
    raw_signals_json: str


class CandidateVoteLogger:
    """Capture policy-annotated directional votes during replay."""

    def __init__(self) -> None:
        self._run_id: Optional[str] = None
        self.rows: list[CandidateRow] = []

    def set_run_context(self, run_id: Optional[str]) -> None:
        self._run_id = str(run_id or "").strip() or None

    def log_vote(self, vote: StrategyVote) -> None:
        if vote.signal_type.value != "ENTRY":
            return
        if vote.direction not in (Direction.CE, Direction.PE):
            return
        checks = vote.raw_signals.get("_policy_checks") if isinstance(vote.raw_signals, dict) else None
        checks = checks if isinstance(checks, dict) else {}
        self.rows.append(
            CandidateRow(
                snapshot_id=vote.snapshot_id,
                trade_date=vote.trade_date,
                timestamp=vote.timestamp.isoformat(),
                strategy_name=vote.strategy_name,
                direction=vote.direction.value,
                vote_confidence=float(vote.confidence),
                vote_reason=vote.reason,
                proposed_strike=vote.proposed_strike,
                proposed_entry_premium=vote.proposed_entry_premium,
                regime=_safe_text(vote.raw_signals.get("_regime")),
                regime_confidence=_safe_float(vote.raw_signals.get("_regime_conf")),
                regime_reason=_safe_text(vote.raw_signals.get("_regime_reason")),
                policy_allowed=_safe_bool(vote.raw_signals.get("_policy_allowed")),
                policy_score=_safe_float(vote.raw_signals.get("_policy_score")),
                policy_reason=_safe_text(vote.raw_signals.get("_policy_reason")),
                policy_check_volume=_safe_text(checks.get("volume")),
                policy_check_momentum=_safe_text(checks.get("momentum")),
                policy_check_timing=_safe_text(checks.get("timing")),
                policy_check_premium=_safe_text(checks.get("premium")),
                policy_check_regime=_safe_text(checks.get("regime")),
                raw_signals_json=json.dumps(vote.raw_signals, default=str, sort_keys=True),
            )
        )

    def log_signal(self, signal: TradeSignal, *, acted_on: bool = True) -> None:  # noqa: ARG002
        return

    def log_position_open(self, signal: TradeSignal, position: PositionContext) -> None:  # noqa: ARG002
        return

    def log_position_manage(self, *, position: PositionContext, timestamp, snapshot_id: str) -> None:  # noqa: ANN001, ARG002
        return

    def log_position_close(self, **kwargs: Any) -> None:  # noqa: ANN401
        return


def _safe_float(value: object) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _safe_text(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _safe_bool(value: object) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return None


def _extract_snapshot_features(snap: SnapshotAccessor) -> dict[str, Any]:
    fut_oi = snap.fut_oi
    fut_oi_change_30m = snap.fut_oi_change_30m
    oi_change_pct = None
    if fut_oi is not None and fut_oi > 0 and fut_oi_change_30m is not None:
        oi_change_pct = float(fut_oi_change_30m) / float(fut_oi)
    return {
        "minutes_since_open": snap.minutes,
        "days_to_expiry": snap.days_to_expiry,
        "is_expiry_day": snap.is_expiry_day,
        "fut_return_5m": snap.fut_return_5m,
        "fut_return_15m": snap.fut_return_15m,
        "fut_return_30m": snap.fut_return_30m,
        "vol_ratio": snap.vol_ratio,
        "realized_vol_30m": snap.realized_vol_30m,
        "pcr": snap.pcr,
        "pcr_change_30m": snap.pcr_change_30m,
        "fut_oi_change_30m": fut_oi_change_30m,
        "fut_oi": fut_oi,
        "oi_change_pct": oi_change_pct,
        "vix_current": snap.vix_current,
        "iv_percentile": snap.iv_percentile,
        "iv_skew": snap.iv_skew,
        "price_vs_vwap": snap.price_vs_vwap,
        "or_width": snap.or_width,
        "price_vs_orh": snap.price_vs_orh,
        "price_vs_orl": snap.price_vs_orl,
        "orh_broken": snap.orh_broken,
        "orl_broken": snap.orl_broken,
        "atm_ce_close": snap.atm_ce_close,
        "atm_pe_close": snap.atm_pe_close,
        "atm_ce_vol_ratio": snap.atm_ce_vol_ratio,
        "atm_pe_vol_ratio": snap.atm_pe_vol_ratio,
        "atm_ce_oi_change_30m": snap.atm_ce_oi_change_30m,
        "atm_pe_oi_change_30m": snap.atm_pe_oi_change_30m,
    }


def _load_snapshots(store: ParquetStore, start_date: str, end_date: str) -> pd.DataFrame:
    df = store.snapshots_for_date_range(start_date, end_date)
    df = enforce_snapshot_schema_version(
        df,
        required_version=REQUIRED_SNAPSHOT_SCHEMA_VERSION,
        context=f"entry_candidate_dataset[{start_date}..{end_date}]",
    )
    if df.empty:
        return df
    frame = df.loc[:, ["trade_date", "timestamp", "snapshot_raw_json"]].copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    return frame


def _future_labels(
    *,
    rows: list[dict[str, Any]],
    index_by_snapshot_id: dict[str, int],
    snapshot_id: str,
    direction: str,
    entry_premium: Optional[float],
    horizons: list[int],
    primary_horizon: int,
    primary_threshold_pct: float,
) -> dict[str, Any]:
    labels: dict[str, Any] = {
        "primary_label_name": "mfe15_gt_5_v1",
        "primary_label_formula": f"max(P[t+1:t+{primary_horizon}]) / P[t] > {1.0 + primary_threshold_pct:.2f}",
    }
    if not entry_premium or entry_premium <= 0:
        labels["label_primary_mfe_hit"] = None
        labels["label_mfe_gt_8pct_15bars"] = None
        labels["label_mfe_gt_5pct_10bars"] = None
        labels["label_pnl_gt_5"] = None
        labels["label_capture_gt_40"] = None
        labels["trade_pnl_pct"] = None
        labels["trade_mfe_pct"] = None
        labels["trade_capture_ratio"] = None
        for horizon in horizons:
            labels[f"future_mfe_{horizon}bars"] = None
            labels[f"future_mae_{horizon}bars"] = None
            labels[f"future_end_return_{horizon}bars"] = None
            labels[f"available_future_bars_{horizon}"] = 0
            labels[f"label_mfe_gt_3pct_{horizon}bars"] = None
            labels[f"label_mfe_gt_5pct_{horizon}bars"] = None
            labels[f"label_mfe_gt_8pct_{horizon}bars"] = None
        return labels

    current_index = index_by_snapshot_id[snapshot_id]
    premium_field = "atm_ce_close" if direction == "CE" else "atm_pe_close"
    for horizon in horizons:
        future_slice = rows[current_index + 1 : current_index + 1 + horizon]
        future_prices = [float(item[premium_field]) for item in future_slice if _safe_float(item.get(premium_field))]
        labels[f"available_future_bars_{horizon}"] = len(future_prices)
        if not future_prices:
            labels[f"future_mfe_{horizon}bars"] = None
            labels[f"future_mae_{horizon}bars"] = None
            labels[f"future_end_return_{horizon}bars"] = None
            labels[f"label_mfe_gt_3pct_{horizon}bars"] = None
            labels[f"label_mfe_gt_5pct_{horizon}bars"] = None
            labels[f"label_mfe_gt_8pct_{horizon}bars"] = None
            continue
        returns = [(price - entry_premium) / entry_premium for price in future_prices]
        mfe = max(returns)
        mae = min(returns)
        end_return = returns[-1]
        labels[f"future_mfe_{horizon}bars"] = mfe
        labels[f"future_mae_{horizon}bars"] = mae
        labels[f"future_end_return_{horizon}bars"] = end_return
        labels[f"label_mfe_gt_3pct_{horizon}bars"] = mfe > 0.03
        labels[f"label_mfe_gt_5pct_{horizon}bars"] = mfe > 0.05
        labels[f"label_mfe_gt_8pct_{horizon}bars"] = mfe > 0.08

    labels["label_primary_mfe_hit"] = labels.get(f"label_mfe_gt_5pct_{primary_horizon}bars")
    labels["label_mfe_gt_8pct_15bars"] = labels.get("label_mfe_gt_8pct_15bars")
    labels["label_mfe_gt_5pct_10bars"] = labels.get("label_mfe_gt_5pct_10bars")
    trade_pnl_pct = _safe_float(labels.get(f"future_end_return_{primary_horizon}bars"))
    trade_mfe_pct = _safe_float(labels.get(f"future_mfe_{primary_horizon}bars"))
    trade_capture_ratio: Optional[float] = None
    if trade_pnl_pct is not None and trade_mfe_pct is not None and trade_mfe_pct > 0.0:
        trade_capture_ratio = float(trade_pnl_pct / trade_mfe_pct)
    labels["trade_pnl_pct"] = trade_pnl_pct
    labels["trade_mfe_pct"] = trade_mfe_pct
    labels["trade_capture_ratio"] = trade_capture_ratio
    labels["label_pnl_gt_5"] = (trade_pnl_pct > 0.05) if trade_pnl_pct is not None else None
    labels["label_capture_gt_40"] = (
        (trade_capture_ratio > 0.40) if trade_capture_ratio is not None else None
    )
    return labels


def _day_rows(snapshot_frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in snapshot_frame.itertuples(index=False):
        payload = json.loads(str(item.snapshot_raw_json))
        snap = SnapshotAccessor(payload)
        feature_row = {
            "snapshot_id": snap.snapshot_id,
            "trade_date": snap.trade_date,
            "timestamp": snap.timestamp.isoformat() if snap.timestamp is not None else None,
            **_extract_snapshot_features(snap),
        }
        rows.append(feature_row)
    return rows


def _bool_positive_rate(series: pd.Series) -> Optional[float]:
    if series is None:
        return None
    if len(series) == 0:
        return None
    vals = series.dropna()
    if len(vals) == 0:
        return None
    try:
        return float(vals.astype(float).mean())
    except Exception:
        return None


def _label_coverage_by_split(
    frame: pd.DataFrame,
    *,
    label_columns: list[str],
) -> dict[str, dict[str, dict[str, Optional[float]]]]:
    out: dict[str, dict[str, dict[str, Optional[float]]]] = {}
    if frame is None or len(frame) == 0:
        return out
    work = frame.copy()
    work["split_name"] = work.get("split_name", pd.Series(dtype="object")).fillna("UNKNOWN").astype(str)
    for split_name, split_df in work.groupby("split_name", sort=True):
        split_rows = int(len(split_df))
        out[str(split_name)] = {}
        for col in label_columns:
            if col not in split_df.columns:
                out[str(split_name)][col] = {
                    "rows": split_rows,
                    "non_null_count": 0,
                    "null_rate": 1.0,
                    "positive_rate": None,
                }
                continue
            vals = split_df[col]
            non_null = int(vals.notna().sum())
            out[str(split_name)][col] = {
                "rows": split_rows,
                "non_null_count": non_null,
                "null_rate": float(1.0 - (non_null / split_rows)) if split_rows > 0 else None,
                "positive_rate": _bool_positive_rate(vals),
            }
    return out


def _build_eda_verification(
    frame: pd.DataFrame,
    *,
    requested_label_columns: list[str],
    label_null_rate_fail_threshold: float,
    label_shift_fail_threshold: float,
    policy_diagnostic_warn_threshold: float,
    dropped_rows_by_reason: dict[str, int],
) -> dict[str, Any]:
    rows = int(len(frame))
    columns = int(len(frame.columns))
    null_rates: dict[str, float] = {}
    for col in frame.columns:
        null_rates[col] = float(frame[col].isna().mean()) if rows > 0 else 0.0
    label_null_rate = {
        col: float(null_rates.get(col, 1.0))
        for col in requested_label_columns
    }

    coverage = _label_coverage_by_split(frame, label_columns=requested_label_columns)
    distribution_shift_checks: dict[str, dict[str, Any]] = {}
    for col in requested_label_columns:
        train_rate = ((coverage.get("train") or {}).get(col) or {}).get("positive_rate")
        split_rates = {
            split_name: ((split_payload.get(col) or {}).get("positive_rate"))
            for split_name, split_payload in coverage.items()
        }
        deltas: dict[str, Optional[float]] = {}
        max_abs_delta: Optional[float] = None
        if train_rate is not None:
            for split_name, rate in split_rates.items():
                if rate is None:
                    deltas[split_name] = None
                    continue
                delta = float(rate - float(train_rate))
                deltas[split_name] = delta
                abs_delta = abs(delta)
                if max_abs_delta is None or abs_delta > max_abs_delta:
                    max_abs_delta = abs_delta
        distribution_shift_checks[col] = {
            "train_positive_rate": train_rate,
            "split_positive_rate": split_rates,
            "delta_vs_train": deltas,
            "max_abs_delta_vs_train": max_abs_delta,
            "shift_gate_pass": (
                max_abs_delta is None or max_abs_delta <= float(label_shift_fail_threshold)
            ),
        }

    constant_columns: list[str] = []
    for col in frame.columns:
        non_null = frame[col].dropna()
        if len(non_null) == 0:
            continue
        if int(non_null.nunique(dropna=True)) <= 1:
            constant_columns.append(str(col))

    policy_diagnostics = [
        "policy_allowed",
        "policy_score",
        "policy_reason",
        "policy_check_volume",
        "policy_check_momentum",
        "policy_check_timing",
        "policy_check_premium",
        "policy_check_regime",
    ]
    warning_rows = []
    for col in policy_diagnostics:
        if col not in null_rates:
            continue
        rate = float(null_rates[col])
        if rate >= float(policy_diagnostic_warn_threshold):
            warning_rows.append(
                {
                    "column": col,
                    "null_rate": rate,
                    "severity": "warning",
                    "message": "high_null_policy_diagnostic",
                }
            )

    label_null_failures = [
        {
            "label_column": col,
            "null_rate": float(rate),
            "threshold": float(label_null_rate_fail_threshold),
        }
        for col, rate in label_null_rate.items()
        if float(rate) >= float(label_null_rate_fail_threshold)
    ]
    label_shift_failures = [
        {
            "label_column": col,
            "max_abs_delta_vs_train": float(check["max_abs_delta_vs_train"]),
            "threshold": float(label_shift_fail_threshold),
        }
        for col, check in distribution_shift_checks.items()
        if check.get("max_abs_delta_vs_train") is not None
        and float(check["max_abs_delta_vs_train"]) > float(label_shift_fail_threshold)
    ]

    return {
        "rows": rows,
        "columns": columns,
        "window_start": (
            str(frame["trade_date"].min()) if rows and "trade_date" in frame.columns else None
        ),
        "window_end": (
            str(frame["trade_date"].max()) if rows and "trade_date" in frame.columns else None
        ),
        "requested_label_columns": list(requested_label_columns),
        "label_coverage_by_split": coverage,
        "label_null_rate": label_null_rate,
        "distribution_shift_checks": distribution_shift_checks,
        "constant_columns": constant_columns,
        "null_rate_profile_top": sorted(
            [{"column": col, "null_rate": float(rate)} for col, rate in null_rates.items()],
            key=lambda x: x["null_rate"],
            reverse=True,
        )[:25],
        "policy_diagnostic_warnings": warning_rows,
        "dropped_rows_by_reason": {str(k): int(v) for k, v in dropped_rows_by_reason.items()},
        "validation_failures": {
            "label_null_rate_failures": label_null_failures,
            "label_shift_failures": label_shift_failures,
        },
        "pretrain_validation_passed": bool(
            len(label_null_failures) == 0 and len(label_shift_failures) == 0
        ),
    }


def build_entry_candidate_dataset(
    *,
    config: EntryQualityDatasetConfig,
    output_root: Path | None = None,
    run_meta: Optional[dict[str, Any]] = None,
    requested_label_columns: Optional[list[str]] = None,
    label_null_rate_fail_threshold: float = 0.95,
    label_shift_fail_threshold: float = 0.12,
    policy_diagnostic_warn_threshold: float = 0.30,
) -> dict[str, Any]:
    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_CANONICAL,
        context="entry_candidate_dataset",
        parquet_base=config.snapshot_base,
        min_day=config.start_date,
        max_day=config.end_date,
    )
    store = ParquetStore(config.snapshot_base, snapshots_dataset=SNAPSHOT_DATASET_CANONICAL)
    snapshots = _load_snapshots(store, config.start_date, config.end_date)
    output_dir = output_root or config.output_root
    output_dir.mkdir(parents=True, exist_ok=True)

    logger = CandidateVoteLogger()
    engine = DeterministicRuleEngine(signal_logger=logger)
    engine.set_run_context(
        "entry-quality-candidates",
        {"risk_config": {}, "policy_config": {}},
    )

    raw_vote_rows: list[CandidateRow] = []
    current_day: Optional[str] = None
    per_day_snapshot_rows: dict[str, list[dict[str, Any]]] = {}
    for trade_date, day_frame in snapshots.groupby("trade_date", sort=True):
        per_day_snapshot_rows[str(trade_date)] = _day_rows(day_frame)
        if current_day is not None:
            engine.on_session_end(date.fromisoformat(current_day))
        engine.on_session_start(date.fromisoformat(str(trade_date)))
        current_day = str(trade_date)
        for item in day_frame.itertuples(index=False):
            payload = json.loads(str(item.snapshot_raw_json))
            engine.evaluate(payload)
        raw_vote_rows.extend(logger.rows)
        logger.rows = []
    if current_day is not None:
        engine.on_session_end(date.fromisoformat(current_day))

    vote_df = pd.DataFrame([row.__dict__ for row in raw_vote_rows])
    if vote_df.empty:
        meta = {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "rows": 0,
            **snapshot_access.to_metadata(),
        }
        (output_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if run_meta is not None:
            (output_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
        return meta

    enriched_rows: list[dict[str, Any]] = []
    dropped_rows_by_reason = {
        "missing_snapshot_row": 0,
        "out_of_window_split": 0,
        "invalid_timestamp": 0,
    }
    for trade_date, group in vote_df.groupby("trade_date", sort=True):
        day_rows = per_day_snapshot_rows.get(str(trade_date), [])
        index_by_snapshot_id = {str(item["snapshot_id"]): idx for idx, item in enumerate(day_rows)}
        snapshot_lookup = {str(item["snapshot_id"]): item for item in day_rows}
        for vote_row in group.itertuples(index=False):
            snap_row = snapshot_lookup.get(str(vote_row.snapshot_id))
            if not isinstance(snap_row, dict):
                dropped_rows_by_reason["missing_snapshot_row"] += 1
                continue
            base = dict(snap_row)
            base.update(vote_row._asdict())
            base["split_name"] = split_name_for_trade_date(str(vote_row.trade_date), config.split)
            if str(base["split_name"]) == "out_of_window":
                dropped_rows_by_reason["out_of_window_split"] += 1
                continue
            base["dataset_version"] = "entry_candidate_labels_v1"
            labels = _future_labels(
                rows=day_rows,
                index_by_snapshot_id=index_by_snapshot_id,
                snapshot_id=str(vote_row.snapshot_id),
                direction=str(vote_row.direction),
                entry_premium=_safe_float(vote_row.proposed_entry_premium),
                horizons=[5, 10, 15],
                primary_horizon=config.primary_horizon_bars,
                primary_threshold_pct=config.primary_threshold_pct,
            )
            base.update(labels)
            enriched_rows.append(base)

    out_df = pd.DataFrame(enriched_rows)
    out_df["timestamp"] = pd.to_datetime(out_df["timestamp"], errors="coerce", utc=True)
    dropped_rows_by_reason["invalid_timestamp"] = int(out_df["timestamp"].isna().sum())
    out_df = out_df[out_df["timestamp"].notna()].copy()
    out_df = out_df.sort_values(["timestamp", "snapshot_id", "strategy_name"], kind="stable").reset_index(drop=True)
    out_path = output_dir / "entry_candidate_labels.parquet"
    out_df.to_parquet(out_path, index=False)

    label_columns = list(requested_label_columns or [])
    if not label_columns:
        label_columns = sorted({profile.column_name for profile in LABEL_PROFILES.values()})
    eda_verification = _build_eda_verification(
        out_df,
        requested_label_columns=label_columns,
        label_null_rate_fail_threshold=float(label_null_rate_fail_threshold),
        label_shift_fail_threshold=float(label_shift_fail_threshold),
        policy_diagnostic_warn_threshold=float(policy_diagnostic_warn_threshold),
        dropped_rows_by_reason=dropped_rows_by_reason,
    )
    (output_dir / "eda_verification.json").write_text(
        json.dumps(eda_verification, indent=2),
        encoding="utf-8",
    )

    profile_summary = {
        "rows": int(len(out_df)),
        "positive_rate_primary": (
            float(out_df["label_primary_mfe_hit"].dropna().astype(float).mean())
            if "label_primary_mfe_hit" in out_df.columns and out_df["label_primary_mfe_hit"].notna().any()
            else None
        ),
        "split_counts": {
            str(key): int(value)
            for key, value in out_df["split_name"].value_counts(dropna=False).to_dict().items()
        },
        "strategy_counts": {
            str(key): int(value)
            for key, value in out_df["strategy_name"].value_counts(dropna=False).to_dict().items()
        },
        "regime_counts": {
            str(key): int(value)
            for key, value in out_df["regime"].fillna("UNKNOWN").value_counts(dropna=False).to_dict().items()
        },
        "dropped_rows_by_reason": {str(k): int(v) for k, v in dropped_rows_by_reason.items()},
    }
    meta = {
        "dataset_version": "entry_candidate_labels_v1",
        "start_date": config.start_date,
        "end_date": config.end_date,
        "required_snapshot_schema_version": REQUIRED_SNAPSHOT_SCHEMA_VERSION,
        **snapshot_access.to_metadata(),
        "primary_horizon_bars": config.primary_horizon_bars,
        "primary_threshold_pct": config.primary_threshold_pct,
        "output_parquet": str(out_path).replace("\\", "/"),
        "profile_summary": profile_summary,
        "label_coverage_by_split": eda_verification["label_coverage_by_split"],
        "label_null_rate": eda_verification["label_null_rate"],
        "constant_columns": eda_verification["constant_columns"],
        "distribution_shift_checks": eda_verification["distribution_shift_checks"],
        "pretrain_validation_passed": bool(eda_verification["pretrain_validation_passed"]),
        "dropped_rows_by_reason": {str(k): int(v) for k, v in dropped_rows_by_reason.items()},
        "split": {
            "train_start": config.split.train_start,
            "train_end": config.split.train_end,
            "valid_start": config.split.valid_start,
            "valid_end": config.split.valid_end,
            "eval_start": config.split.eval_start,
            "eval_end": config.split.eval_end,
        },
    }
    (output_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (output_dir / "profile_summary.json").write_text(json.dumps(profile_summary, indent=2), encoding="utf-8")
    if run_meta is not None:
        (output_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
    formal_run = bool((run_meta or {}).get("formal_run"))
    if formal_run and not bool(eda_verification["pretrain_validation_passed"]):
        failures = eda_verification.get("validation_failures") or {}
        raise ValueError(
            "entry-candidate pretrain validation failed for formal run: "
            f"null_rate_failures={len(failures.get('label_null_rate_failures') or [])}, "
            f"shift_failures={len(failures.get('label_shift_failures') or [])}"
        )
    return meta


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build canonical entry-candidate dataset for entry-quality ML.")
    parser.add_argument("--snapshot-base", default=str(EntryQualityDatasetConfig().snapshot_base))
    parser.add_argument("--start-date", default=DEFAULT_CONTINUOUS_START)
    parser.add_argument("--end-date", default=DEFAULT_CONTINUOUS_END)
    parser.add_argument("--out-root", default=str(DEFAULT_CANDIDATE_ROOT))
    parser.add_argument("--primary-horizon-bars", type=int, default=15)
    parser.add_argument("--primary-threshold-pct", type=float, default=0.05)
    parser.add_argument("--train-start", default=EntryQualitySplitConfig().train_start)
    parser.add_argument("--train-end", default=EntryQualitySplitConfig().train_end)
    parser.add_argument("--valid-start", default=EntryQualitySplitConfig().valid_start)
    parser.add_argument("--valid-end", default=EntryQualitySplitConfig().valid_end)
    parser.add_argument("--eval-start", default=EntryQualitySplitConfig().eval_start)
    parser.add_argument("--eval-end", default=EntryQualitySplitConfig().eval_end)
    parser.add_argument("--window-manifest", default=None, help="Path to canonical window manifest JSON.")
    parser.add_argument("--formal-run", action="store_true", help="Enforce formal readiness rules from window manifest.")
    parser.add_argument("--manifest-min-trading-days", type=int, default=DEFAULT_MIN_TRADING_DAYS)
    parser.add_argument("--manifest-required-schema-version", default=DEFAULT_REQUIRED_SCHEMA_VERSION)
    parser.add_argument(
        "--label-profiles",
        default=",".join(sorted(LABEL_PROFILES.keys())),
        help="Comma-separated label profile ids used for pretrain label quality gates.",
    )
    parser.add_argument("--label-null-rate-fail-threshold", type=float, default=0.95)
    parser.add_argument("--label-shift-fail-threshold", type=float, default=0.12)
    parser.add_argument("--policy-diagnostic-warn-threshold", type=float, default=0.30)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.formal_run and not args.window_manifest:
        raise SystemExit("--formal-run requires --window-manifest")

    manifest_meta: Optional[dict[str, Any]] = None
    start_date = str(args.start_date)
    end_date = str(args.end_date)
    split = EntryQualitySplitConfig(
        train_start=str(args.train_start),
        train_end=str(args.train_end),
        valid_start=str(args.valid_start),
        valid_end=str(args.valid_end),
        eval_start=str(args.eval_start),
        eval_end=str(args.eval_end),
    )
    if args.window_manifest:
        manifest_meta = load_and_validate_window_manifest(
            args.window_manifest,
            formal_run=bool(args.formal_run),
            required_schema_version=str(args.manifest_required_schema_version),
            min_trading_days=int(args.manifest_min_trading_days),
            context="entry_candidate_dataset.window_manifest",
        )
        start_date = str(manifest_meta["window_start"])
        end_date = str(manifest_meta["window_end"])
        store = ParquetStore(Path(args.snapshot_base), snapshots_dataset=SNAPSHOT_DATASET_CANONICAL)
        split_days = store.available_snapshot_days(start_date, end_date)
        split_map = split_boundaries_for_days(split_days)
        split = EntryQualitySplitConfig(
            train_start=split_map["train_start"],
            train_end=split_map["train_end"],
            valid_start=split_map["valid_start"],
            valid_end=split_map["valid_end"],
            eval_start=split_map["eval_start"],
            eval_end=split_map["eval_end"],
        )

    config = EntryQualityDatasetConfig(
        snapshot_base=Path(args.snapshot_base),
        start_date=start_date,
        end_date=end_date,
        output_root=Path(args.out_root),
        primary_horizon_bars=int(args.primary_horizon_bars),
        primary_threshold_pct=float(args.primary_threshold_pct),
        split=split,
    )
    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_CANONICAL,
        context="entry_candidate_dataset",
        parquet_base=Path(args.snapshot_base),
        min_day=config.start_date,
        max_day=config.end_date,
    )
    run_meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": "strategy_app.offline_ml.entry_candidate_dataset",
        "formal_run": bool(args.formal_run),
        "exploratory_only": bool((manifest_meta or {}).get("exploratory_only", not bool(args.formal_run))),
        "window_manifest": manifest_meta,
        "manifest_path": (manifest_meta or {}).get("manifest_path"),
        "manifest_hash": (manifest_meta or {}).get("manifest_hash"),
        "window_start": config.start_date,
        "window_end": config.end_date,
        **snapshot_access.to_metadata(),
        "split_boundaries": {
            "train_start": config.split.train_start,
            "train_end": config.split.train_end,
            "valid_start": config.split.valid_start,
            "valid_end": config.split.valid_end,
            "eval_start": config.split.eval_start,
            "eval_end": config.split.eval_end,
        },
        "gate_results": {
            "formal_ready": (manifest_meta or {}).get("formal_ready"),
            "required_schema_version": str(args.manifest_required_schema_version),
            "min_trading_days_required": int(args.manifest_min_trading_days),
            "window_trading_days": (manifest_meta or {}).get("trading_days"),
            "all_days_required_schema": (manifest_meta or {}).get("all_days_required_schema"),
        },
    }
    requested_profile_ids = [item.strip() for item in str(args.label_profiles).split(",") if item.strip()]
    requested_label_columns = []
    for profile_id in requested_profile_ids:
        profile = LABEL_PROFILES.get(profile_id)
        if profile is None:
            raise SystemExit(f"unknown label profile: {profile_id}")
        requested_label_columns.append(str(profile.column_name))
    result = build_entry_candidate_dataset(
        config=config,
        output_root=Path(args.out_root),
        run_meta=run_meta,
        requested_label_columns=requested_label_columns,
        label_null_rate_fail_threshold=float(args.label_null_rate_fail_threshold),
        label_shift_fail_threshold=float(args.label_shift_fail_threshold),
        policy_diagnostic_warn_threshold=float(args.policy_diagnostic_warn_threshold),
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
