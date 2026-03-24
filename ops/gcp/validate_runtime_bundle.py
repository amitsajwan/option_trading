#!/usr/bin/env python3
"""Preflight validation for runtime release, publish, and startup bundles."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_PAPER_DAYS = 10
MIN_SHADOW_DAYS = 10
MAX_CAPPED_LIVE_SIZE_MULTIPLIER = 0.25
REQUIRED_RELEASE_KEYS = ("STRATEGY_ENGINE", "ML_PURE_RUN_ID", "ML_PURE_MODEL_GROUP")
REQUIRED_LIVE_KEYS = (
    "STRATEGY_ENGINE",
    "STRATEGY_ROLLOUT_STAGE",
    "STRATEGY_POSITION_SIZE_MULTIPLIER",
    "STRATEGY_ML_RUNTIME_GUARD_FILE",
)
REQUIRED_IMAGE_KEYS = (
    "GHCR_IMAGE_PREFIX",
    "APP_IMAGE_TAG",
)


class ValidationError(ValueError):
    """Raised when a runtime bundle fails preflight validation."""


@dataclass(frozen=True)
class ValidationResult:
    mode: str
    repo_root: Path
    env_file: Path
    status: str
    checks: list[str]
    details: dict[str, Any]


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValidationError(f"env file not found: {path}")
    values: dict[str, str] = {}
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValidationError(f"{path}:{line_no} is not a KEY=VALUE entry")
        key, value = line.split("=", 1)
        values[str(key).strip()] = str(value).strip()
    return values


def _require_nonempty(values: dict[str, str], keys: tuple[str, ...], *, label: str) -> None:
    missing = [key for key in keys if not str(values.get(key, "")).strip()]
    if missing:
        raise ValidationError(f"{label} missing keys: {', '.join(missing)}")


def _repo_relative_path(repo_root: Path, raw_path: str, *, field_name: str) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ValidationError(f"{field_name} is empty")
    candidate = Path(text)
    if candidate.is_absolute():
        raise ValidationError(f"{field_name} must be repo-relative, got absolute path: {text}")
    resolved = (repo_root / candidate).resolve()
    repo_root_resolved = repo_root.resolve()
    try:
        resolved.relative_to(repo_root_resolved)
    except ValueError as exc:
        raise ValidationError(f"{field_name} must stay under repo root: {text}") from exc
    return resolved


def _require_existing_repo_relative_path(repo_root: Path, raw_path: str, *, field_name: str) -> Path:
    resolved = _repo_relative_path(repo_root, raw_path, field_name=field_name)
    if not resolved.exists():
        raise ValidationError(f"{field_name} does not exist: {resolved}")
    return resolved


def _load_guard_payload(guard_path: Path) -> dict[str, Any]:
    if not guard_path.is_file():
        raise ValidationError(f"guard file not found: {guard_path}")
    payload = json.loads(guard_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValidationError("runtime guard payload must be a JSON object")

    approved = bool(payload.get("approved_for_runtime"))
    strict_positive = bool(payload.get("offline_strict_positive_passed"))
    try:
        paper_days = int(payload.get("paper_days_observed") or 0)
        shadow_days = int(payload.get("shadow_days_observed") or 0)
    except Exception as exc:
        raise ValidationError("runtime guard paper/shadow day counts must be integers") from exc

    if not approved:
        raise ValidationError("runtime guard rejected: approved_for_runtime=false")
    if not strict_positive:
        raise ValidationError("runtime guard rejected: offline_strict_positive_passed=false")
    if paper_days < MIN_PAPER_DAYS:
        raise ValidationError(f"runtime guard rejected: paper_days_observed<{MIN_PAPER_DAYS}")
    if shadow_days < MIN_SHADOW_DAYS:
        raise ValidationError(f"runtime guard rejected: shadow_days_observed<{MIN_SHADOW_DAYS}")

    return {
        "approved_for_runtime": approved,
        "offline_strict_positive_passed": strict_positive,
        "paper_days_observed": paper_days,
        "shadow_days_observed": shadow_days,
        "guard_path": str(guard_path.resolve()),
    }


def _validate_handoff_mode(*, repo_root: Path, release_env_path: Path, env_file: Path) -> ValidationResult:
    release_values = _load_env_file(release_env_path)
    _require_nonempty(release_values, REQUIRED_RELEASE_KEYS, label="release env")

    if str(release_values["STRATEGY_ENGINE"]).strip().lower() != "ml_pure":
        raise ValidationError("release env must set STRATEGY_ENGINE=ml_pure")

    _ = _load_env_file(env_file)
    return ValidationResult(
        mode="handoff",
        repo_root=repo_root,
        env_file=env_file,
        status="ok",
        checks=[
            "release env present",
            "handoff keys present",
            "compose env parseable",
        ],
        details={
            "strategy_engine": "ml_pure",
            "run_id": str(release_values["ML_PURE_RUN_ID"]).strip(),
            "model_group": str(release_values["ML_PURE_MODEL_GROUP"]).strip(),
        },
    )


def _validate_runtime_mode(*, repo_root: Path, env_file: Path) -> ValidationResult:
    values = _load_env_file(env_file)
    _require_nonempty(values, REQUIRED_IMAGE_KEYS, label="compose env")
    strategy_engine = str(values.get("STRATEGY_ENGINE") or "").strip().lower()
    checks: list[str] = ["compose env parseable", "runtime image source fields present"]

    if strategy_engine != "ml_pure":
        return ValidationResult(
            mode="runtime",
            repo_root=repo_root,
            env_file=env_file,
            status="ok",
            checks=checks + ["non-ml_pure runtime, live ML checks skipped"],
            details={"strategy_engine": strategy_engine or None},
        )

    _require_nonempty(values, REQUIRED_LIVE_KEYS, label="compose env")
    checks.extend(
        [
            "ml_pure rollout fields present",
            "guard file present and valid",
        ]
    )

    run_id = str(values.get("ML_PURE_RUN_ID") or "").strip()
    model_group = str(values.get("ML_PURE_MODEL_GROUP") or "").strip()
    model_package = str(values.get("ML_PURE_MODEL_PACKAGE") or "").strip()
    threshold_report = str(values.get("ML_PURE_THRESHOLD_REPORT") or "").strip()
    training_summary = str(values.get("ML_PURE_TRAINING_SUMMARY_PATH") or "").strip()
    rollout_stage = str(values.get("STRATEGY_ROLLOUT_STAGE") or "").strip().lower()
    guard_file = str(values.get("STRATEGY_ML_RUNTIME_GUARD_FILE") or "").strip()
    size_multiplier_raw = str(values.get("STRATEGY_POSITION_SIZE_MULTIPLIER") or "").strip()

    run_mode = bool(run_id or model_group)
    explicit_mode = bool(model_package)
    threshold_anchor_mode = bool(threshold_report) and run_mode and not model_package
    if run_mode and explicit_mode:
        raise ValidationError("ml_pure runtime cannot mix run-id mode and explicit model-package path")
    if not run_mode and not explicit_mode:
        raise ValidationError(
            "ml_pure runtime requires ML_PURE_RUN_ID+ML_PURE_MODEL_GROUP or "
            "ML_PURE_MODEL_PACKAGE+ML_PURE_THRESHOLD_REPORT"
        )
    if run_mode and (not run_id or not model_group):
        raise ValidationError("ml_pure run-id mode requires both ML_PURE_RUN_ID and ML_PURE_MODEL_GROUP")
    if explicit_mode and not threshold_report:
        raise ValidationError(
            "ml_pure explicit-path mode requires both ML_PURE_MODEL_PACKAGE and ML_PURE_THRESHOLD_REPORT"
        )
    if threshold_anchor_mode:
        checks.append("threshold report anchor preserved for run-id mode")
    if rollout_stage != "capped_live":
        raise ValidationError("ml_pure runtime requires STRATEGY_ROLLOUT_STAGE=capped_live")

    try:
        size_multiplier = float(size_multiplier_raw)
    except Exception as exc:
        raise ValidationError("STRATEGY_POSITION_SIZE_MULTIPLIER must be numeric") from exc
    if size_multiplier <= 0.0:
        raise ValidationError("ml_pure runtime requires STRATEGY_POSITION_SIZE_MULTIPLIER > 0")
    if size_multiplier > MAX_CAPPED_LIVE_SIZE_MULTIPLIER:
        raise ValidationError(
            f"ml_pure runtime requires STRATEGY_POSITION_SIZE_MULTIPLIER <= {MAX_CAPPED_LIVE_SIZE_MULTIPLIER}"
        )

    guard_path = _require_existing_repo_relative_path(
        repo_root,
        guard_file,
        field_name="STRATEGY_ML_RUNTIME_GUARD_FILE",
    )
    guard_details = _load_guard_payload(guard_path)
    if model_package:
        _require_existing_repo_relative_path(repo_root, model_package, field_name="ML_PURE_MODEL_PACKAGE")
    if threshold_report:
        _require_existing_repo_relative_path(repo_root, threshold_report, field_name="ML_PURE_THRESHOLD_REPORT")
    if training_summary:
        _require_existing_repo_relative_path(
            repo_root,
            training_summary,
            field_name="ML_PURE_TRAINING_SUMMARY_PATH",
        )

    return ValidationResult(
        mode="runtime",
        repo_root=repo_root,
        env_file=env_file,
        status="ok",
        checks=checks,
        details={
            "strategy_engine": "ml_pure",
            "run_id": run_id,
            "model_group": model_group,
            "rollout_stage": rollout_stage,
            "position_size_multiplier": size_multiplier,
            "guard_file": str(guard_path),
            "guard": guard_details,
            "model_package": model_package or None,
            "threshold_report": threshold_report or None,
            "training_summary": training_summary or None,
        },
    )


def validate_runtime_bundle(*, mode: str, repo_root: Path, env_file: Path, release_env_path: Path | None = None) -> ValidationResult:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"handoff", "runtime"}:
        raise ValidationError(f"unsupported validation mode: {normalized_mode}")
    if not repo_root.is_dir():
        raise ValidationError(f"repo root not found: {repo_root}")
    if normalized_mode == "handoff":
        if release_env_path is None:
            raise ValidationError("handoff validation requires release_env_path")
        return _validate_handoff_mode(repo_root=repo_root, release_env_path=release_env_path, env_file=env_file)
    return _validate_runtime_mode(repo_root=repo_root, env_file=env_file)


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate deploy/runtime bundle inputs")
    parser.add_argument("--mode", choices=["handoff", "runtime"], required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--release-env-path", default=None)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    env_file = Path(args.env_file or (repo_root / ".env.compose")).resolve()
    release_env_path = Path(args.release_env_path).resolve() if args.release_env_path else None

    try:
        result = validate_runtime_bundle(
            mode=str(args.mode),
            repo_root=repo_root,
            env_file=env_file,
            release_env_path=release_env_path,
        )
    except ValidationError as exc:
        payload = {
            "status": "failed",
            "mode": str(args.mode),
            "repo_root": str(repo_root),
            "env_file": str(env_file),
            "error": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(result.__dict__, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
