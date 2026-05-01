from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from contracts_app import isoformat_ist

from ..logging.jsonl_sink import append_jsonl

logger = logging.getLogger(__name__)

ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_RUNTIME_ARTIFACT_DIR = Path(".run") / "strategy_app"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return isoformat_ist(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _now_ist() -> str:
    return isoformat_ist(datetime.now(tz=timezone.utc))


@dataclass(frozen=True)
class RuntimeArtifactPaths:
    root: Path
    config_path: Path
    state_path: Path
    metrics_path: Path
    operator_halt_path: Path


def resolve_runtime_artifact_paths(artifact_dir: Optional[Path | str] = None) -> RuntimeArtifactPaths:
    if artifact_dir is not None:
        root = Path(artifact_dir)
    else:
        env_root = (
            os.getenv("STRATEGY_RUNTIME_ARTIFACT_DIR")
            or os.getenv("STRATEGY_RUN_DIR")
        )
        root = Path(env_root) if env_root else DEFAULT_RUNTIME_ARTIFACT_DIR
    root = root.resolve()
    return RuntimeArtifactPaths(
        root=root,
        config_path=root / "runtime_config.json",
        state_path=root / "runtime_state.json",
        metrics_path=root / "metrics.jsonl",
        operator_halt_path=root / "operator_halt",
    )


def _read_json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("artifact payload must be a JSON object")
    return payload


def _artifact_file_payload(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
    }
    if not path.exists():
        return payload
    try:
        stat = path.stat()
        payload["size_bytes"] = int(stat.st_size)
        payload["modified_at_ist"] = isoformat_ist(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))
        payload["payload"] = _read_json_file(path)
    except Exception as exc:
        payload["error"] = str(exc)
    return payload


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(rendered + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _metrics_summary(path: Path, tail_lines: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "line_count": 0,
        "tail": [],
        "latest": None,
    }
    if not path.exists():
        return summary

    tail_records: deque[dict[str, Any]] = deque(maxlen=max(1, int(tail_lines)))
    line_count = 0
    parse_errors = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                line_count += 1
                try:
                    parsed = json.loads(line)
                except Exception:
                    parse_errors += 1
                    tail_records.append({"raw": line, "parse_error": True})
                    continue
                if isinstance(parsed, dict):
                    tail_records.append(parsed)
                else:
                    tail_records.append({"raw": line, "parse_error": True})
    except Exception as exc:
        summary["error"] = str(exc)
        return summary

    summary["line_count"] = line_count
    summary["tail"] = list(tail_records)
    summary["latest"] = tail_records[-1] if tail_records else None
    if parse_errors:
        summary["parse_errors"] = parse_errors
    try:
        stat = path.stat()
        summary["modified_at_ist"] = isoformat_ist(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))
        summary["size_bytes"] = int(stat.st_size)
    except Exception:
        pass
    return summary


def _nested_dict(payload: Optional[dict[str, Any]], key: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def summarize_runtime_artifacts(
    config_payload: Optional[dict[str, Any]],
    state_payload: Optional[dict[str, Any]],
    metrics_payload: dict[str, Any],
) -> dict[str, Any]:
    config = config_payload if isinstance(config_payload, dict) else {}
    state = state_payload if isinstance(state_payload, dict) else {}
    config_model = _nested_dict(config, "model")
    config_rollout = _nested_dict(config, "rollout")
    config_launch = _nested_dict(config, "launch")
    config_ml_pure = _nested_dict(config, "ml_pure")
    state_session = _nested_dict(state, "session")
    state_risk = _nested_dict(state, "risk")
    state_position = _nested_dict(state, "position")
    latest_metric = metrics_payload.get("latest")
    if not isinstance(latest_metric, dict):
        tail = list(metrics_payload.get("tail") or [])
        latest_metric = tail[-1] if tail and isinstance(tail[-1], dict) else {}

    rollout_stage = config_rollout.get("stage")
    if rollout_stage is None:
        rollout_stage = config_launch.get("rollout_stage")

    block_expiry = config_model.get("block_expiry")
    if block_expiry is None:
        block_expiry = config_ml_pure.get("block_expiry")

    run_id = config_model.get("run_id")
    if run_id is None:
        run_id = config_ml_pure.get("run_id")

    model_group = config_model.get("model_group")
    if model_group is None:
        model_group = config_ml_pure.get("model_group")

    has_position = state_position.get("has_position")
    if has_position is None:
        has_position = state.get("has_position")

    latest_event = latest_metric.get("event") if isinstance(latest_metric, dict) else None
    latest_timestamp = latest_metric.get("ts") if isinstance(latest_metric, dict) else None

    return {
        "run_id": run_id,
        "model_group": model_group,
        "strategy_profile_id": config.get("strategy_profile_id") if isinstance(config, dict) else None,
        "rollout_stage": rollout_stage,
        "block_expiry": block_expiry,
        "bars_evaluated": state_session.get("bars_evaluated"),
        "entries_taken": state_session.get("entries_taken"),
        "last_entry_at": state_session.get("last_entry_at"),
        "hold_counts": state_session.get("hold_counts"),
        "hold_rate": state_session.get("hold_rate"),
        "has_position": has_position,
        "is_halted": state_risk.get("is_halted"),
        "is_paused": state_risk.get("is_paused"),
        "session_pnl_pct": state_risk.get("session_pnl_pct"),
        "consecutive_losses": state_risk.get("consecutive_losses"),
        "metrics_line_count": metrics_payload.get("line_count"),
        "metrics_latest_event": latest_event,
        "metrics_latest_timestamp": latest_timestamp,
        "metrics_last_event": latest_event,
        "metrics_last_timestamp": latest_timestamp,
    }


class RuntimeArtifactStore:
    def __init__(self, artifact_dir: Optional[Path | str] = None) -> None:
        self._paths = resolve_runtime_artifact_paths(artifact_dir)

    @property
    def paths(self) -> RuntimeArtifactPaths:
        return self._paths

    def write_config(self, payload: dict[str, Any]) -> None:
        try:
            _write_json_file(self._paths.config_path, payload)
        except Exception:
            logger.exception("failed to write runtime config path=%s", self._paths.config_path)

    def write_state(self, payload: dict[str, Any]) -> None:
        try:
            _write_json_file(self._paths.state_path, payload)
        except Exception:
            logger.exception("failed to write runtime state path=%s", self._paths.state_path)

    def append_metric(self, payload: dict[str, Any]) -> None:
        try:
            append_jsonl(self._paths.metrics_path, payload, logger=logger)
        except Exception:
            logger.exception("failed to append runtime metric path=%s", self._paths.metrics_path)

    def read_config(self) -> dict[str, Any]:
        return _artifact_file_payload(self._paths.config_path)

    def read_state(self) -> dict[str, Any]:
        return _artifact_file_payload(self._paths.state_path)

    def read_metrics(self, *, tail_lines: int = 10) -> dict[str, Any]:
        return _metrics_summary(self._paths.metrics_path, tail_lines)


def build_runtime_config_payload(
    *,
    engine: str,
    topic: str,
    strategy_profile_id: str,
    runtime_artifact_dir: Path,
    signal_run_dir: Path,
    min_confidence: float,
    rollout_stage: str,
    position_size_multiplier: float,
    halt_consecutive_losses: int,
    halt_daily_dd_pct: float,
    run_id: Optional[str],
    model_group: Optional[str],
    model_package_path: Optional[str],
    threshold_report_path: Optional[str],
    guard_file: Optional[str],
    block_expiry: bool,
    ml_pure_max_feature_age_sec: int,
    ml_pure_max_nan_features: int,
    ml_pure_max_hold_bars: int,
    ml_pure_min_oi: float,
    ml_pure_min_volume: float,
) -> dict[str, Any]:
    model_payload = {
        "run_id": run_id,
        "model_group": model_group,
        "model_package_path": model_package_path,
        "threshold_report_path": threshold_report_path,
        "guard_file": guard_file,
        "block_expiry": bool(block_expiry),
    }
    rollout_payload = {
        "stage": str(rollout_stage),
        "min_confidence": float(min_confidence),
        "position_size_multiplier": float(position_size_multiplier),
        "halt_consecutive_losses": int(halt_consecutive_losses),
        "halt_daily_dd_pct": float(halt_daily_dd_pct),
    }
    return {
        "artifact_type": "strategy_runtime_config",
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "checked_at_ist": _now_ist(),
        "engine": str(engine),
        "topic": str(topic),
        "strategy_profile_id": str(strategy_profile_id),
        "runtime_artifact_dir": str(runtime_artifact_dir),
        "signal_run_dir": str(signal_run_dir),
        "model": model_payload,
        "rollout": rollout_payload,
        "launch": {
            "min_confidence": float(min_confidence),
            "rollout_stage": str(rollout_stage),
            "position_size_multiplier": float(position_size_multiplier),
            "halt_consecutive_losses": int(halt_consecutive_losses),
            "halt_daily_dd_pct": float(halt_daily_dd_pct),
        },
        "ml_pure": {
            "block_expiry": bool(block_expiry),
            "max_feature_age_sec": int(ml_pure_max_feature_age_sec),
            "max_nan_features": int(ml_pure_max_nan_features),
            "max_hold_bars": int(ml_pure_max_hold_bars),
            "min_oi": float(ml_pure_min_oi),
            "min_volume": float(ml_pure_min_volume),
        },
    }


def build_runtime_state_payload(
    *,
    engine: str,
    strategy_profile_id: str,
    runtime_artifact_dir: Path,
    run_id: Optional[str],
    model_group: Optional[str],
    block_expiry: bool,
    max_feature_age_sec: int,
    max_nan_features: int,
    max_hold_bars: int,
    min_oi: float,
    min_volume: float,
    session_trade_date: Optional[str],
    session_started_at_ist: Optional[str],
    session_updated_at_ist: Optional[str],
    bars_evaluated: int,
    entries_taken: int,
    last_entry_at: Optional[str],
    hold_counts: dict[str, int],
    is_halted: bool,
    is_paused: bool,
    session_pnl_pct: Optional[float],
    consecutive_losses: int,
    has_position: bool,
    current_position: Optional[dict[str, Any]] = None,
    last_event: Optional[dict[str, Any]] = None,
    last_decision: Optional[dict[str, Any]] = None,
    warmup: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    hold_total = sum(int(value) for value in hold_counts.values())
    return {
        "artifact_type": "strategy_runtime_state",
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "checked_at_ist": _now_ist(),
        "engine": str(engine),
        "strategy_profile_id": str(strategy_profile_id),
        "runtime_artifact_dir": str(runtime_artifact_dir),
        "run_id": run_id,
        "model_group": model_group,
        "runtime": {
            "block_expiry": bool(block_expiry),
            "max_feature_age_sec": int(max_feature_age_sec),
            "max_nan_features": int(max_nan_features),
            "max_hold_bars": int(max_hold_bars),
            "min_oi": float(min_oi),
            "min_volume": float(min_volume),
        },
        "session": {
            "trade_date": session_trade_date,
            "started_at_ist": session_started_at_ist,
            "updated_at_ist": session_updated_at_ist,
            "bars_evaluated": int(bars_evaluated),
            "entries_taken": int(entries_taken),
            "last_entry_at": last_entry_at,
            "hold_counts": dict(sorted(hold_counts.items())),
            "hold_total": int(hold_total),
            "hold_rate": (round(float(hold_total) / float(bars_evaluated), 4) if int(bars_evaluated) > 0 else None),
            "warmup": warmup,
        },
        "risk": {
            "is_halted": bool(is_halted),
            "is_paused": bool(is_paused),
            "session_pnl_pct": session_pnl_pct,
            "consecutive_losses": int(consecutive_losses),
        },
        "position": {
            "has_position": bool(has_position),
            "current": current_position,
        },
        "has_position": bool(has_position),
        "last_event": last_event,
        "last_decision": last_decision,
    }
