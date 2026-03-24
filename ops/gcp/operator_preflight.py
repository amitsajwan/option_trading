#!/usr/bin/env python3
"""Shared preflight checks for interactive GCP operator flows."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _THIS_FILE = Path(__file__).resolve()
    _REPO_ROOT = _THIS_FILE.parents[2]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

from ops.gcp.runtime_release_manifest import MANIFEST_KIND
from ops.gcp.validate_runtime_bundle import ValidationError, validate_runtime_bundle


class OperatorPreflightError(ValueError):
    """Raised when operator preflight cannot pass."""


@dataclass
class PreflightResult:
    mode: str
    status: str
    checks: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def _load_key_value_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise OperatorPreflightError(f"env file not found: {path}")
    values: dict[str, str] = {}
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise OperatorPreflightError(f"{path}:{line_no} is not KEY=VALUE")
        key, value = line.split("=", 1)
        values[str(key).strip()] = str(value).strip().strip('"').strip("'")
    return values


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise OperatorPreflightError(f"expected JSON object: {path}")
    return payload


def _kite_credentials_status(credentials_path: Path) -> tuple[str, str]:
    if not credentials_path.exists():
        return "missing", f"credentials file not found: {credentials_path}"
    try:
        payload = _load_json(credentials_path)
    except Exception as exc:
        return "stale_or_unreadable", f"credentials unreadable: {exc}"

    api_key = str(payload.get("api_key") or "").strip()
    access_token = str(payload.get("access_token") or "").strip()
    if not api_key or not access_token:
        return "stale_or_unreadable", "credentials missing api_key or access_token"
    return "present", "credentials file has api_key and access_token"


def _run_command(command: list[str]) -> tuple[int, str]:
    proc = subprocess.run(command, capture_output=True, text=True)
    output = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode, output


def _validate_manifest(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if str(payload.get("kind") or "").strip() != MANIFEST_KIND:
        raise OperatorPreflightError(f"release manifest has wrong kind: {path}")
    if str(payload.get("release_status") or "").strip().lower() != "published":
        raise OperatorPreflightError(f"release manifest is not published: {path}")
    required = [
        "run_id",
        "model_group",
        "app_image_tag",
        "runtime_guard_path",
        "threshold_report",
        "training_summary",
        "runtime_env_path",
    ]
    missing = [key for key in required if not str(payload.get(key) or "").strip()]
    if missing:
        raise OperatorPreflightError(f"release manifest missing keys: {', '.join(missing)}")
    return payload


def _check_ghcr_images(prefix: str, tag: str, *, services: list[str]) -> tuple[bool, list[str], list[str]]:
    ok: list[str] = []
    missing: list[str] = []
    for svc in services:
        code, _ = _run_command(["docker", "manifest", "inspect", f"{prefix}/{svc}:{tag}"])
        if code == 0:
            ok.append(svc)
        else:
            missing.append(svc)
    return not missing, ok, missing


def _historical_dates_available_from_reports(
    *, parquet_base: Path, start_date: str, end_date: str
) -> tuple[bool, str]:
    reports_root = parquet_base / "reports"
    coverage_audit_path = reports_root / "coverage_audit.json"
    if coverage_audit_path.is_file():
        payload = _load_json(coverage_audit_path)
        built_days = payload.get("built_days")
        if isinstance(built_days, dict):
            min_day = str(built_days.get("min") or "").strip()
            max_day = str(built_days.get("max") or "").strip()
            buildable_missing_count = int(payload.get("buildable_missing_count") or 0)
            source_missing_count = int(payload.get("source_missing_count") or 0)
            if (
                min_day
                and max_day
                and start_date >= min_day
                and end_date <= max_day
                and buildable_missing_count == 0
                and source_missing_count == 0
            ):
                return True, "historical replay dates covered by coverage_audit.json"
            return False, "requested dates fall outside built_days coverage or coverage audit reports gaps"

    window_manifest_path = reports_root / "window_manifest_latest.json"
    if window_manifest_path.is_file():
        payload = _load_json(window_manifest_path)
        window_start = str(payload.get("window_start") or "").strip()
        window_end = str(payload.get("window_end") or "").strip()
        if window_start and window_end and start_date >= window_start and end_date <= window_end:
            return True, "historical replay dates covered by window_manifest_latest.json"
        return False, "requested dates fall outside window_manifest_latest.json coverage"

    return False, "no coverage report available for historical date validation"


def _validate_infra_mode(*, operator_env_file: Path) -> PreflightResult:
    result = PreflightResult(mode="infra", status="ready")
    values = _load_key_value_file(operator_env_file)
    required = [
        "PROJECT_ID",
        "REGION",
        "ZONE",
        "RUNTIME_NAME",
        "MODEL_BUCKET_NAME",
        "RUNTIME_CONFIG_BUCKET_NAME",
    ]
    missing = [key for key in required if not str(values.get(key) or "").strip()]
    if missing:
        result.status = "blocked"
        result.blockers.append(f"operator env missing keys: {', '.join(missing)}")
        return result
    result.checks.append("operator env present")
    result.checks.append("infra keys present")
    result.details["project_id"] = values.get("PROJECT_ID")
    result.details["runtime_name"] = values.get("RUNTIME_NAME")
    return result


def _validate_live_mode(
    *,
    repo_root: Path,
    env_file: Path,
    manifest_path: Path,
    ghcr_image_prefix: str,
    credentials_path: Path,
) -> PreflightResult:
    result = PreflightResult(mode="live", status="ready")
    manifest = _validate_manifest(manifest_path)
    result.checks.append("release manifest present")
    result.details["run_id"] = manifest["run_id"]
    result.details["model_group"] = manifest["model_group"]
    result.details["app_image_tag"] = manifest["app_image_tag"]

    runtime_result = validate_runtime_bundle(mode="runtime", repo_root=repo_root, env_file=env_file)
    result.checks.extend(runtime_result.checks)
    result.details["runtime_bundle"] = runtime_result.details

    kite_status, kite_detail = _kite_credentials_status(credentials_path)
    result.details["kite_credentials_status"] = kite_status
    result.details["kite_credentials_detail"] = kite_detail
    if kite_status != "present":
        result.status = "blocked"
        result.blockers.append(f"kite credentials {kite_status}: {kite_detail}")
    else:
        result.checks.append("kite credentials present")

    if not str(ghcr_image_prefix or "").strip():
        result.status = "blocked"
        result.blockers.append("GHCR image prefix is empty")
        return result
    if not str(manifest.get("app_image_tag") or "").strip():
        result.status = "blocked"
        result.blockers.append("release manifest app_image_tag is empty")
        return result

    if shutil_which("docker") is None:
        result.status = "blocked"
        result.blockers.append("docker not found on operator host; cannot verify GHCR image tag")
        return result
    ok, ok_svcs, missing_svcs = _check_ghcr_images(
        str(ghcr_image_prefix).strip(),
        str(manifest["app_image_tag"]).strip(),
        services=[
            "ingestion_app",
            "snapshot_app",
            "persistence_app",
            "strategy_app",
            "market_data_dashboard",
            "strategy_eval_orchestrator",
            "strategy_eval_ui",
        ],
    )
    if not ok:
        result.status = "blocked"
        result.blockers.append(f"missing GHCR images for tag {manifest['app_image_tag']}: {', '.join(missing_svcs)}")
    else:
        result.checks.append("GHCR images present")
        result.details["ghcr_checked_services"] = ok_svcs
    return result


def _validate_historical_mode(
    *,
    env_file: Path,
    snapshot_parquet_bucket_url: str,
    start_date: str,
    end_date: str,
    parquet_base: Path | None,
) -> PreflightResult:
    result = PreflightResult(mode="historical", status="ready")
    env_values = _load_key_value_file(env_file)
    live_topic = str(env_values.get("LIVE_TOPIC") or "").strip()
    historical_topic = str(env_values.get("HISTORICAL_TOPIC") or "").strip()
    if historical_topic != "market:snapshot:v1:historical":
        result.status = "blocked"
        result.blockers.append(f"HISTORICAL_TOPIC must be market:snapshot:v1:historical, got {historical_topic or 'missing'}")
    elif historical_topic == live_topic:
        result.status = "blocked"
        result.blockers.append("historical topic resolves to live topic")
    else:
        result.checks.append("historical topic isolated")

    collection_pairs = [
        ("MONGO_COLL_SNAPSHOTS", "MONGO_COLL_SNAPSHOTS_HISTORICAL"),
        ("MONGO_COLL_STRATEGY_VOTES", "MONGO_COLL_STRATEGY_VOTES_HISTORICAL"),
        ("MONGO_COLL_TRADE_SIGNALS", "MONGO_COLL_TRADE_SIGNALS_HISTORICAL"),
        ("MONGO_COLL_STRATEGY_POSITIONS", "MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL"),
    ]
    coll_errors = [
        f"{live_key} and {hist_key} collide"
        for live_key, hist_key in collection_pairs
        if str(env_values.get(live_key) or "").strip()
        and str(env_values.get(live_key) or "").strip() == str(env_values.get(hist_key) or "").strip()
    ]
    if coll_errors:
        result.status = "blocked"
        result.blockers.extend(coll_errors)
    else:
        result.checks.append("historical mongo collections isolated")

    if not str(snapshot_parquet_bucket_url or "").strip().startswith("gs://"):
        result.status = "blocked"
        result.blockers.append("SNAPSHOT_PARQUET_BUCKET_URL must start with gs://")
    else:
        result.checks.append("snapshot parquet bucket configured")

    if parquet_base is not None:
        try:
            from snapshot_app.historical.parquet_store import ParquetStore

            store = ParquetStore(parquet_base, snapshots_dataset="snapshots")
            available_days = set(store.available_snapshot_days(min_day=start_date, max_day=end_date))
            requested_days = {start_date, end_date}
            if requested_days - available_days:
                ok, detail = _historical_dates_available_from_reports(
                    parquet_base=parquet_base,
                    start_date=start_date,
                    end_date=end_date,
                )
                if ok:
                    result.checks.append("historical replay dates covered by parquet reports")
                    result.details["parquet_date_check"] = detail
                else:
                    result.status = "blocked"
                    result.blockers.append(
                        f"historical replay date missing from parquet: {', '.join(sorted(requested_days - available_days))}"
                    )
            else:
                result.checks.append("historical replay dates available in parquet")
        except (ImportError, ModuleNotFoundError, RuntimeError):
            ok, detail = _historical_dates_available_from_reports(
                parquet_base=parquet_base,
                start_date=start_date,
                end_date=end_date,
            )
            if ok:
                result.checks.append("historical replay dates covered by parquet reports")
                result.details["parquet_date_check"] = detail
            else:
                result.status = "blocked"
                result.blockers.append(f"historical replay date missing from parquet: {detail}")
    else:
        result.details["parquet_date_check"] = "skipped"
    return result


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def validate_operator_preflight(
    *,
    mode: str,
    repo_root: Path,
    env_file: Path | None = None,
    operator_env_file: Path | None = None,
    release_manifest_path: Path | None = None,
    ghcr_image_prefix: str = "",
    credentials_path: Path | None = None,
    snapshot_parquet_bucket_url: str = "",
    start_date: str = "",
    end_date: str = "",
    parquet_base: Path | None = None,
) -> PreflightResult:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "infra":
        if operator_env_file is None:
            raise OperatorPreflightError("infra preflight requires operator_env_file")
        return _validate_infra_mode(operator_env_file=operator_env_file)
    if normalized_mode == "live":
        if env_file is None or release_manifest_path is None or credentials_path is None:
            raise OperatorPreflightError("live preflight requires env_file, release_manifest_path, and credentials_path")
        return _validate_live_mode(
            repo_root=repo_root,
            env_file=env_file,
            manifest_path=release_manifest_path,
            ghcr_image_prefix=ghcr_image_prefix,
            credentials_path=credentials_path,
        )
    if normalized_mode == "historical":
        if env_file is None:
            raise OperatorPreflightError("historical preflight requires env_file")
        return _validate_historical_mode(
            env_file=env_file,
            snapshot_parquet_bucket_url=snapshot_parquet_bucket_url,
            start_date=start_date,
            end_date=end_date,
            parquet_base=parquet_base,
        )
    raise OperatorPreflightError(f"unsupported mode: {mode}")


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate GCP operator preflight readiness")
    parser.add_argument("--mode", choices=["infra", "live", "historical"], required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--operator-env-file", default=None)
    parser.add_argument("--release-manifest-path", default=None)
    parser.add_argument("--ghcr-image-prefix", default="")
    parser.add_argument("--credentials-path", default=None)
    parser.add_argument("--snapshot-parquet-bucket-url", default="")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--parquet-base", default=None)
    args = parser.parse_args(argv)

    try:
        result = validate_operator_preflight(
            mode=str(args.mode),
            repo_root=Path(args.repo_root).resolve(),
            env_file=Path(args.env_file).resolve() if args.env_file else None,
            operator_env_file=Path(args.operator_env_file).resolve() if args.operator_env_file else None,
            release_manifest_path=Path(args.release_manifest_path).resolve() if args.release_manifest_path else None,
            ghcr_image_prefix=str(args.ghcr_image_prefix or ""),
            credentials_path=Path(args.credentials_path).resolve() if args.credentials_path else None,
            snapshot_parquet_bucket_url=str(args.snapshot_parquet_bucket_url or ""),
            start_date=str(args.start_date or ""),
            end_date=str(args.end_date or ""),
            parquet_base=Path(args.parquet_base).resolve() if args.parquet_base else None,
        )
    except (OperatorPreflightError, ValidationError, FileNotFoundError, RuntimeError) as exc:
        payload = {
            "mode": str(args.mode),
            "status": "blocked",
            "checks": [],
            "blockers": [str(exc)],
            "details": {},
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if result.status == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
