"""Strategy app entrypoint (Layer 4 consumer)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

from contracts_app import configure_ist_logging, snapshot_topic

from .contracts import SignalType, TradeSignal
from .engines import DeterministicRuleEngine, PureMLEngine
from .engines.runtime_artifacts import (
    RuntimeArtifactStore,
    build_runtime_config_payload,
    resolve_runtime_artifact_paths,
)
from .logging.signal_logger import SignalLogger
from .runtime import RedisSnapshotConsumer

logger = logging.getLogger(__name__)
MIN_PAPER_DAYS = 10
MIN_SHADOW_DAYS = 10
MAX_CAPPED_LIVE_SIZE_MULTIPLIER = 0.25


def _normalize_optional_str(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _build_signal_handler() -> Callable[[TradeSignal], None]:
    def handle_signal(signal: TradeSignal) -> None:
        if signal.signal_type == SignalType.ENTRY:
            logger.info(
                "signal entry dir=%s strike=%s premium=%.2f lots=%d conf=%.2f reason=%s",
                signal.direction,
                signal.strike,
                signal.entry_premium or 0.0,
                signal.max_lots,
                signal.confidence or 0.0,
                signal.reason,
            )
        elif signal.signal_type == SignalType.EXIT:
            logger.info(
                "signal exit position=%s reason=%s detail=%s",
                signal.position_id,
                signal.exit_reason.value if signal.exit_reason else "",
                signal.reason,
            )

    return handle_signal


def _resolve_ml_runtime_guard_file(cli_guard_file: Optional[str]) -> Optional[str]:
    guard_path = _normalize_optional_str(cli_guard_file)
    if guard_path:
        return guard_path
    return _normalize_optional_str(os.getenv("ML_RUNTIME_GUARD_FILE"))


def _resolve_ml_pure_model_package(cli_value: Optional[str]) -> Optional[str]:
    value = _normalize_optional_str(cli_value)
    if value is not None:
        return value
    return _normalize_optional_str(os.getenv("ML_PURE_MODEL_PACKAGE"))


def _resolve_ml_pure_threshold_report(cli_value: Optional[str]) -> Optional[str]:
    value = _normalize_optional_str(cli_value)
    if value is not None:
        return value
    return _normalize_optional_str(os.getenv("ML_PURE_THRESHOLD_REPORT"))


def _resolve_ml_pure_int(cli_value: Optional[int], env_key: str, default: int) -> int:
    if cli_value is not None:
        return int(cli_value)
    raw = _normalize_optional_str(os.getenv(env_key))
    if raw is None:
        return int(default)
    return int(raw)


def _resolve_ml_pure_float(cli_value: Optional[float], env_key: str, default: float) -> float:
    if cli_value is not None:
        return float(cli_value)
    raw = _normalize_optional_str(os.getenv(env_key))
    if raw is None:
        return float(default)
    return float(raw)


def _resolve_ml_pure_run_id(cli_value: Optional[str]) -> Optional[str]:
    value = _normalize_optional_str(cli_value)
    if value is not None:
        return value
    return _normalize_optional_str(os.getenv("ML_PURE_RUN_ID"))


def _resolve_ml_pure_model_group(cli_value: Optional[str]) -> Optional[str]:
    value = _normalize_optional_str(cli_value)
    if value is not None:
        return value
    return _normalize_optional_str(os.getenv("ML_PURE_MODEL_GROUP"))


def _resolve_strategy_profile_id(cli_value: Optional[str]) -> Optional[str]:
    value = _normalize_optional_str(cli_value)
    if value is not None:
        return value
    return _normalize_optional_str(os.getenv("STRATEGY_PROFILE_ID"))


def _load_json_file(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("runtime guard payload must be a JSON object")
    return payload


def _enforce_ml_runtime_guard(
    *,
    rollout_stage: str,
    position_size_multiplier: float,
    guard_file: Optional[str],
    runtime_enabled: Optional[bool] = None,
) -> None:
    enabled = bool(runtime_enabled)
    if not enabled:
        return
    stage = str(rollout_stage or "").strip().lower()
    if stage != "capped_live":
        raise ValueError("ml runtime is allowed only in capped_live stage after paper/shadow completion")
    if float(position_size_multiplier) > MAX_CAPPED_LIVE_SIZE_MULTIPLIER:
        raise ValueError(f"capped_live ml runtime requires position_size_multiplier <= {MAX_CAPPED_LIVE_SIZE_MULTIPLIER}")
    if not guard_file:
        raise ValueError("ml runtime guard requires --ml-runtime-guard-file or ML_RUNTIME_GUARD_FILE")

    payload = _load_json_file(guard_file)
    approved = bool(payload.get("approved_for_runtime"))
    strict_positive = bool(payload.get("offline_strict_positive_passed"))
    paper_days = int(payload.get("paper_days_observed") or 0)
    shadow_days = int(payload.get("shadow_days_observed") or 0)

    if not approved:
        raise ValueError("ml runtime guard rejected: approved_for_runtime=false")
    if not strict_positive:
        raise ValueError("ml runtime guard rejected: offline_strict_positive_passed=false")
    if paper_days < MIN_PAPER_DAYS:
        raise ValueError(f"ml runtime guard rejected: paper_days_observed<{MIN_PAPER_DAYS}")
    if shadow_days < MIN_SHADOW_DAYS:
        raise ValueError(f"ml runtime guard rejected: shadow_days_observed<{MIN_SHADOW_DAYS}")


def _load_model_switch_resolver():
    try:
        from ml_pipeline_2.publishing.resolver import resolve_ml_pure_artifacts, validate_switch_strict

        return resolve_ml_pure_artifacts, validate_switch_strict
    except Exception:
        repo_root = Path(__file__).resolve().parents[1]
        ml2_src = (repo_root / "ml_pipeline_2" / "src").resolve()
        if ml2_src.exists() and str(ml2_src) not in sys.path:
            sys.path.insert(0, str(ml2_src))
        try:
            from ml_pipeline_2.publishing.resolver import resolve_ml_pure_artifacts, validate_switch_strict

            return resolve_ml_pure_artifacts, validate_switch_strict
        except Exception:
            pass
    raise ImportError("ml_pipeline_2 publishing resolver unavailable; ml_pure run-id mode requires ml_pipeline_2")


def _resolve_ml_pure_switch_paths(
    *,
    engine_key: str,
    run_id: Optional[str],
    model_group: Optional[str],
    model_package: Optional[str],
    threshold_report: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[dict[str, str]]]:
    if (run_id or model_group) and engine_key != "ml_pure":
        raise ValueError("--ml-pure-run-id/--ml-pure-model-group can only be used with --engine ml_pure")
    explicit_mode = bool(model_package or threshold_report)
    run_mode = bool(run_id or model_group)
    if run_mode and explicit_mode:
        raise ValueError("ml pure switch conflict: use either run-id mode or explicit package/threshold paths, not both")
    if run_mode:
        if not run_id:
            raise ValueError("ml pure run-id mode requires --ml-pure-run-id (or ML_PURE_RUN_ID)")
        if not model_group:
            raise ValueError("ml pure run-id mode requires --ml-pure-model-group (or ML_PURE_MODEL_GROUP)")
        resolve_ml_pure_artifacts, validate_switch_strict = _load_model_switch_resolver()
        resolved = resolve_ml_pure_artifacts(str(run_id), str(model_group))
        report_payload = resolved.get("run_report_payload")
        ok, reason = validate_switch_strict(report_payload if isinstance(report_payload, dict) else {})
        logger.info(
            "ml_pure run-id switch run_id=%s model_group=%s run_report=%s validation=%s reason=%s",
            str(run_id),
            str(model_group),
            str(resolved.get("run_report_path") or ""),
            str(ok).lower(),
            str(reason),
        )
        if not ok:
            raise ValueError(f"strict switch blocked: {reason}")
        return (
            str(resolved.get("model_package_path") or ""),
            str(resolved.get("threshold_report_path") or ""),
            {
                "run_id": str(run_id),
                "model_group": str(model_group),
                "run_report_path": str(resolved.get("run_report_path") or ""),
            },
        )
    return model_package, threshold_report, None


def build_engine(
    *,
    engine_name: str,
    min_confidence: float,
    signal_logger: SignalLogger,
    ml_pure_model_package: Optional[str] = None,
    ml_pure_threshold_report: Optional[str] = None,
    ml_pure_max_feature_age_sec: int = 90,
    ml_pure_max_nan_features: int = 3,
    ml_pure_max_hold_bars: int = 15,
    ml_pure_min_oi: float = 50000.0,
    ml_pure_min_volume: float = 15000.0,
    runtime_artifact_dir: Optional[Path | str] = None,
    strategy_profile_id: Optional[str] = None,
):
    engine_key = str(engine_name or "").strip().lower()
    if engine_key == "ml_pure":
        model_package = _normalize_optional_str(ml_pure_model_package)
        threshold_report = _normalize_optional_str(ml_pure_threshold_report)
        if not model_package:
            raise ValueError("ml pure runtime requires --ml-pure-model-package or ML_PURE_MODEL_PACKAGE")
        if not threshold_report:
            raise ValueError("ml pure runtime requires --ml-pure-threshold-report or ML_PURE_THRESHOLD_REPORT")
        return PureMLEngine(
            model_package_path=model_package,
            threshold_report_path=threshold_report,
            max_feature_age_sec=int(ml_pure_max_feature_age_sec),
            max_nan_features=int(ml_pure_max_nan_features),
            max_hold_bars=int(ml_pure_max_hold_bars),
            min_oi=float(ml_pure_min_oi),
            min_volume=float(ml_pure_min_volume),
            signal_logger=signal_logger,
            runtime_artifact_dir=runtime_artifact_dir,
            strategy_profile_id=strategy_profile_id,
        )

    if engine_key == "deterministic":
        return DeterministicRuleEngine(
            min_confidence=float(min_confidence),
            signal_logger=signal_logger,
            engine_mode="deterministic",
            strategy_profile_id=(strategy_profile_id or "det_core_v1"),
        )
    raise ValueError(f"unsupported engine: {engine_name}")


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Strategy app redis consumer runtime.")
    parser.add_argument("--engine", choices=["deterministic", "ml_pure"], default="deterministic")
    parser.add_argument("--topic", default=None, help=f"Snapshot topic (default: {snapshot_topic()})")
    parser.add_argument("--poll-interval-sec", type=float, default=0.2)
    parser.add_argument("--max-events", type=int, default=0, help="Stop after N events (0 = infinite)")
    parser.add_argument("--min-confidence", type=float, default=0.65)
    parser.add_argument("--run-dir", default=None, help="Override strategy JSONL run directory")
    parser.add_argument("--ml-pure-model-package", default=None, help="Joblib model package path for pure ML runtime")
    parser.add_argument("--ml-pure-threshold-report", default=None, help="JSON threshold report path for pure ML runtime")
    parser.add_argument("--ml-pure-run-id", default=None, help="Run id to auto-resolve model package + threshold report (strict PROMOTE only)")
    parser.add_argument("--ml-pure-model-group", default=None, help="Model group used with --ml-pure-run-id (e.g. banknifty_futures/h15_tp_auto)")
    parser.add_argument("--ml-pure-max-feature-age-sec", type=int, default=None, help="Maximum allowed snapshot staleness in seconds for scoring")
    parser.add_argument("--ml-pure-max-nan-features", type=int, default=None, help="Maximum allowed NaN required features before hold")
    parser.add_argument("--ml-pure-max-hold-bars", type=int, default=None, help="Max bars to hold ML-pure positions before time-stop")
    parser.add_argument("--ml-pure-min-oi", type=float, default=None, help="Minimum option OI gate for ML-pure entries")
    parser.add_argument("--ml-pure-min-volume", type=float, default=None, help="Minimum option volume gate for ML-pure entries")
    parser.add_argument("--ml-runtime-guard-file", default=None, help="JSON approval artifact required to enable ML runtime")
    parser.add_argument("--rollout-stage", choices=["paper", "shadow", "capped_live"], default="paper")
    parser.add_argument("--position-size-multiplier", type=float, default=1.0)
    parser.add_argument("--halt-consecutive-losses", type=int, default=3)
    parser.add_argument("--halt-daily-dd-pct", type=float, default=-0.75)
    parser.add_argument("--strategy-profile-id", default=None, help="Versioned strategy profile id for replay comparability")
    args = parser.parse_args(list(argv) if argv is not None else None)

    ml_pure_model_package = _resolve_ml_pure_model_package(args.ml_pure_model_package)
    ml_pure_threshold_report = _resolve_ml_pure_threshold_report(args.ml_pure_threshold_report)
    ml_pure_run_id = _resolve_ml_pure_run_id(args.ml_pure_run_id)
    ml_pure_model_group = _resolve_ml_pure_model_group(args.ml_pure_model_group)
    ml_pure_max_feature_age_sec = _resolve_ml_pure_int(args.ml_pure_max_feature_age_sec, "ML_PURE_MAX_FEATURE_AGE_SEC", 90)
    ml_pure_max_nan_features = _resolve_ml_pure_int(args.ml_pure_max_nan_features, "ML_PURE_MAX_NAN_FEATURES", 3)
    ml_pure_max_hold_bars = _resolve_ml_pure_int(args.ml_pure_max_hold_bars, "ML_PURE_MAX_HOLD_BARS", 15)
    ml_pure_min_oi = _resolve_ml_pure_float(args.ml_pure_min_oi, "ML_PURE_MIN_OI", 50000.0)
    ml_pure_min_volume = _resolve_ml_pure_float(args.ml_pure_min_volume, "ML_PURE_MIN_VOLUME", 15000.0)
    engine_key = str(args.engine or "").strip().lower()
    ml_pure_switch_meta = None
    ml_pure_model_package, ml_pure_threshold_report, ml_pure_switch_meta = _resolve_ml_pure_switch_paths(
        engine_key=engine_key,
        run_id=ml_pure_run_id,
        model_group=ml_pure_model_group,
        model_package=ml_pure_model_package,
        threshold_report=ml_pure_threshold_report,
    )
    strategy_profile_id = _resolve_strategy_profile_id(args.strategy_profile_id)
    if strategy_profile_id is None:
        strategy_profile_id = None if engine_key == "ml_pure" else "det_core_v1"
    ml_runtime_guard_file = _resolve_ml_runtime_guard_file(args.ml_runtime_guard_file)
    runtime_ml_enabled = engine_key == "ml_pure" and bool(ml_pure_model_package)
    runtime_artifact_paths = resolve_runtime_artifact_paths(Path(args.run_dir) if args.run_dir else None)
    _enforce_ml_runtime_guard(
        rollout_stage=str(args.rollout_stage),
        position_size_multiplier=float(args.position_size_multiplier),
        guard_file=ml_runtime_guard_file,
        runtime_enabled=runtime_ml_enabled,
    )
    signal_logger = SignalLogger(runtime_artifact_paths.root)
    engine = build_engine(
        engine_name=str(args.engine),
        min_confidence=float(args.min_confidence),
        signal_logger=signal_logger,
        ml_pure_model_package=ml_pure_model_package,
        ml_pure_threshold_report=ml_pure_threshold_report,
        ml_pure_max_feature_age_sec=ml_pure_max_feature_age_sec,
        ml_pure_max_nan_features=ml_pure_max_nan_features,
        ml_pure_max_hold_bars=ml_pure_max_hold_bars,
        ml_pure_min_oi=ml_pure_min_oi,
        ml_pure_min_volume=ml_pure_min_volume,
        runtime_artifact_dir=runtime_artifact_paths.root,
        strategy_profile_id=strategy_profile_id,
    )
    if str(args.rollout_stage) == "capped_live" and float(args.position_size_multiplier) > 0.25:
        raise SystemExit("--position-size-multiplier must be <= 0.25 for capped_live stage")
    if hasattr(engine, "set_run_context"):
        engine.set_run_context(
            f"runtime-{args.rollout_stage}",
            {
                "risk_config": {
                    "rollout_stage": str(args.rollout_stage),
                    "position_size_multiplier": float(args.position_size_multiplier),
                    "halt_consecutive_losses": int(args.halt_consecutive_losses),
                    "halt_daily_dd_pct": float(args.halt_daily_dd_pct),
                },
                "model_run_id": (ml_pure_switch_meta or {}).get("run_id"),
                "strategy_profile_id": strategy_profile_id,
                "model_group": (ml_pure_switch_meta or {}).get("model_group"),
            },
        )
    topic = str(args.topic or snapshot_topic()).strip() or snapshot_topic()
    runtime_store = RuntimeArtifactStore(runtime_artifact_paths.root)
    runtime_store.write_config(
        build_runtime_config_payload(
            engine=str(args.engine),
            topic=topic,
            strategy_profile_id=str(getattr(engine, "_strategy_profile_id", strategy_profile_id) or ""),
            runtime_artifact_dir=runtime_artifact_paths.root,
            signal_run_dir=runtime_artifact_paths.root,
            min_confidence=float(args.min_confidence),
            rollout_stage=str(args.rollout_stage),
            position_size_multiplier=float(args.position_size_multiplier),
            halt_consecutive_losses=int(args.halt_consecutive_losses),
            halt_daily_dd_pct=float(args.halt_daily_dd_pct),
            run_id=(ml_pure_switch_meta or {}).get("run_id"),
            model_group=(ml_pure_switch_meta or {}).get("model_group"),
            model_package_path=ml_pure_model_package,
            threshold_report_path=ml_pure_threshold_report,
            guard_file=ml_runtime_guard_file,
            block_expiry=bool(getattr(getattr(engine, "_runtime_controls", None), "block_expiry", False)),
            ml_pure_max_feature_age_sec=ml_pure_max_feature_age_sec,
            ml_pure_max_nan_features=ml_pure_max_nan_features,
            ml_pure_max_hold_bars=ml_pure_max_hold_bars,
            ml_pure_min_oi=ml_pure_min_oi,
            ml_pure_min_volume=ml_pure_min_volume,
        )
    )
    consumer = RedisSnapshotConsumer(
        engine=engine,
        topic=topic,
        poll_interval_sec=max(0.01, float(args.poll_interval_sec)),
        on_signal=_build_signal_handler(),
    )
    max_events = None if int(args.max_events) <= 0 else int(args.max_events)
    logger.info(
        "strategy_app starting engine=%s topic=%s min_confidence=%.2f rollout_stage=%s size_multiplier=%.2f ml_pure_run_id=%s ml_pure_model_group=%s ml_pure_model_package=%s ml_pure_threshold_report=%s ml_pure_max_feature_age_sec=%d ml_pure_max_nan_features=%d strategy_profile_id=%s",
        args.engine,
        topic,
        float(args.min_confidence),
        str(args.rollout_stage),
        float(args.position_size_multiplier),
        (ml_pure_switch_meta or {}).get("run_id", "disabled"),
        (ml_pure_switch_meta or {}).get("model_group", "disabled"),
        ml_pure_model_package or "disabled",
        ml_pure_threshold_report or "disabled",
        int(ml_pure_max_feature_age_sec),
        int(ml_pure_max_nan_features),
        strategy_profile_id,
    )
    consumed = consumer.start(max_events=max_events)
    logger.info("strategy_app consumed events=%s", consumed)
    return 0


if __name__ == "__main__":
    configure_ist_logging(level=logging.INFO)
    raise SystemExit(run_cli())
