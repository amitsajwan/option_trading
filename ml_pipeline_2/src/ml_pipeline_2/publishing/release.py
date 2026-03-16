from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ..contracts.manifests import load_and_resolve_manifest
from ..experiment_control.runner import run_research
from ..experiment_control.state import utc_now
from ..run_recovery_threshold_sweep import sweep_recovery_thresholds
from .publish import published_models_root, repo_root


def _load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_env(path: Path, payload: Dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={value}" for key, value in payload.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _selected_primary_row(summary_payload: Dict[str, Any]) -> Dict[str, Any]:
    selected_id = str(summary_payload.get("selected_primary_recipe_id") or "").strip()
    if not selected_id:
        raise ValueError("recovery summary missing selected_primary_recipe_id")
    for row in list(summary_payload.get("primary_recipes") or []):
        if not isinstance(row, dict):
            continue
        recipe = row.get("recipe")
        recipe_id = str((recipe or {}).get("recipe_id") or "").strip() if isinstance(recipe, dict) else ""
        if recipe_id == selected_id:
            return dict(row)
    raise ValueError(f"selected primary recipe not found in summary: {selected_id}")


def _threshold_sweep_summary_path(training_report_path: Path) -> Path:
    return training_report_path.parent / "threshold_sweep" / "summary.json"


def _normalize_threshold_source(value: str) -> str:
    normalized = str(value or "training").strip().lower()
    if normalized not in {"training", "threshold_sweep_recommended"}:
        raise ValueError(f"unsupported threshold_source: {value}")
    return normalized


def _release_metrics_from_training(
    *,
    holdout_summary: Dict[str, Any],
) -> Dict[str, Any]:
    stage_eval = dict(holdout_summary.get("stage_eval") or {})
    promotion_gates = dict(stage_eval.get("promotion_gates") or {})
    promotion_decision = dict(stage_eval.get("promotion_decision") or {})
    return {
        "source": "training",
        "promotion_decision": str(promotion_decision.get("decision") or "").strip().upper(),
        "promotion_eligible": bool(promotion_gates.get("promotion_eligible")),
        "stage_a_passed": bool(holdout_summary.get("stage_a_passed")),
        "side_share_in_band": bool(holdout_summary.get("side_share_in_band")),
        "profit_factor": float(holdout_summary.get("profit_factor", 0.0) or 0.0),
        "net_return_sum": float(holdout_summary.get("net_return_sum", 0.0) or 0.0),
        "trades": int(holdout_summary.get("trades", 0) or 0),
        "long_share": holdout_summary.get("long_share"),
        "short_share": holdout_summary.get("short_share"),
    }


def _release_metrics_from_threshold_sweep(
    *,
    threshold_sweep_summary_path: Path,
) -> Dict[str, Any]:
    if not threshold_sweep_summary_path.exists():
        raise FileNotFoundError(f"threshold sweep summary not found: {threshold_sweep_summary_path}")
    sweep_summary = _load_json(threshold_sweep_summary_path)
    recommended_row = dict(sweep_summary.get("recommended_row") or {})
    if not recommended_row:
        raise ValueError(f"threshold sweep summary missing recommended_row: {threshold_sweep_summary_path}")
    promotion_eligible = bool(recommended_row.get("promotion_eligible"))
    return {
        "source": "threshold_sweep_recommended",
        "promotion_decision": "PROMOTE" if promotion_eligible else "HOLD",
        "promotion_eligible": promotion_eligible,
        "stage_a_passed": bool(recommended_row.get("stage_a_passed")),
        "side_share_in_band": bool(recommended_row.get("side_share_in_band")),
        "profit_factor": float(recommended_row.get("profit_factor", 0.0) or 0.0),
        "net_return_sum": float(recommended_row.get("net_return_sum", 0.0) or 0.0),
        "trades": int(recommended_row.get("trades", 0) or 0),
        "threshold": float(sweep_summary.get("recommended_threshold")),
        "long_share": recommended_row.get("long_share"),
        "short_share": recommended_row.get("short_share"),
        "threshold_sweep_summary_path": str(threshold_sweep_summary_path.resolve()),
        "threshold_sweep_row": recommended_row,
    }


def assess_recovery_release_candidate(
    *,
    run_dir: str | Path,
    threshold_source: str = "training",
) -> Dict[str, Any]:
    source_run_dir = Path(run_dir).resolve()
    summary_path = source_run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"recovery summary.json not found: {summary_path}")
    summary = _load_json(summary_path)
    run_status = str(summary.get("status") or "").strip().lower()
    selected_row = _selected_primary_row(summary)
    training_report = dict(selected_row.get("training_report") or {})
    training_report_path = Path(str(selected_row.get("training_report_path") or "")).resolve()
    if not training_report:
        if not training_report_path.exists():
            raise FileNotFoundError(f"selected primary training report not found: {training_report_path}")
        training_report = _load_json(training_report_path)
    holdout_summary = dict(selected_row.get("holdout_summary") or {})
    best_experiment = dict(training_report.get("best_experiment") or {})
    leaderboard = list(training_report.get("leaderboard") or [])
    leaderboard_top = dict(leaderboard[0] or {}) if leaderboard else {}
    normalized_threshold_source = _normalize_threshold_source(threshold_source)
    threshold_sweep_path = _threshold_sweep_summary_path(training_report_path)

    if normalized_threshold_source == "training":
        release_metrics = _release_metrics_from_training(holdout_summary=holdout_summary)
    else:
        release_metrics = _release_metrics_from_threshold_sweep(
            threshold_sweep_summary_path=threshold_sweep_path,
        )

    utility_constraints_pass = bool(leaderboard_top.get("utility_constraints_pass"))
    selected_by_fallback = bool(best_experiment.get("selected_by_fallback"))
    gates = {
        "completed_run": run_status == "completed",
        "promotion_eligible": bool(release_metrics.get("promotion_eligible")),
        "stage_a_passed": bool(release_metrics.get("stage_a_passed")),
        "positive_net_return": float(release_metrics.get("net_return_sum", 0.0) or 0.0) > 0.0,
        "side_share_in_band": bool(release_metrics.get("side_share_in_band")),
        "utility_constraints_pass": utility_constraints_pass,
        "selected_without_fallback": not selected_by_fallback,
    }
    blocking_reasons = []
    if not gates["completed_run"]:
        blocking_reasons.append(f"run_status={run_status or 'missing'}")
    if not gates["promotion_eligible"]:
        blocking_reasons.append(f"promotion_decision={release_metrics.get('promotion_decision') or 'HOLD'}")
    if not gates["stage_a_passed"]:
        blocking_reasons.append("stage_a_failed")
    if not gates["positive_net_return"]:
        blocking_reasons.append(f"net_return_sum={release_metrics.get('net_return_sum', 0.0)}")
    if not gates["side_share_in_band"]:
        blocking_reasons.append("side_share_out_of_band")
    if not gates["utility_constraints_pass"]:
        blocking_reasons.append("utility_constraints_failed")
    if not gates["selected_without_fallback"]:
        blocking_reasons.append("selected_by_fallback")

    return {
        "created_at_utc": utc_now(),
        "run_dir": str(source_run_dir),
        "run_id": str(source_run_dir.name),
        "selected_primary_recipe_id": str(summary.get("selected_primary_recipe_id") or ""),
        "threshold_source": normalized_threshold_source,
        "publishable": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
        "gates": gates,
        "release_metrics": release_metrics,
        "training_quality": {
            "selected_by_fallback": selected_by_fallback,
            "utility_constraints_pass": utility_constraints_pass,
            "feature_profile": training_report.get("feature_profile"),
            "objective": training_report.get("objective"),
            "label_target": training_report.get("label_target"),
        },
        "source_paths": {
            "summary": str(summary_path.resolve()),
            "training_report": str(training_report_path.resolve()) if training_report_path else None,
            "threshold_sweep_summary": str(threshold_sweep_path.resolve()) if threshold_sweep_path.exists() else None,
        },
    }


def sync_published_model_group_to_gcs(
    *,
    model_bucket_url: str,
    model_group: str,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    bucket_url = str(model_bucket_url or "").strip().rstrip("/")
    group = str(model_group or "").strip().strip("/\\")
    if not bucket_url:
        raise ValueError("model_bucket_url must be non-empty")
    if not group:
        raise ValueError("model_group must be non-empty")
    publish_root = repo_root(root)
    source_path = (published_models_root(root=publish_root) / Path(group)).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"published model group not found: {source_path}")
    target_url = f"{bucket_url}/{Path(group).as_posix()}"
    cmd = ["gcloud", "storage", "rsync", str(source_path), target_url, "--recursive"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "gcloud storage rsync failed: "
            f"exit={result.returncode} stderr={result.stderr.strip() or result.stdout.strip()}"
        )
    return {
        "status": "completed",
        "command": cmd,
        "source_path": str(source_path),
        "target_url": target_url,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def release_recovery_run(
    *,
    config: Optional[str | Path] = None,
    run_dir: Optional[str | Path] = None,
    model_group: str,
    profile_id: str,
    threshold_source: str = "threshold_sweep_recommended",
    threshold_grid: Optional[Sequence[float]] = None,
    run_output_root: Optional[Path] = None,
    model_bucket_url: Optional[str] = None,
    allow_unsafe_publish: bool = False,
    skip_threshold_sweep: bool = False,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    if bool(config) == bool(run_dir):
        raise ValueError("exactly one of config or run_dir must be provided")

    research_summary: Optional[Dict[str, Any]] = None
    normalized_threshold_source = _normalize_threshold_source(threshold_source)
    if config is not None:
        manifest_path = Path(config).resolve()
        resolved = load_and_resolve_manifest(manifest_path, validate_paths=True)
        research_summary = run_research(
            resolved,
            run_output_root=(Path(run_output_root).resolve() if run_output_root is not None else None),
        )
        resolved_run_dir = Path(str(research_summary["output_root"])).resolve()
    else:
        resolved_run_dir = Path(str(run_dir)).resolve()

    threshold_sweep_payload: Optional[Dict[str, Any]] = None
    if not skip_threshold_sweep and (normalized_threshold_source == "threshold_sweep_recommended" or threshold_grid is not None):
        threshold_sweep_payload = sweep_recovery_thresholds(
            run_dir=resolved_run_dir,
            threshold_grid=threshold_grid,
        )

    assessment = assess_recovery_release_candidate(
        run_dir=resolved_run_dir,
        threshold_source=normalized_threshold_source,
    )
    release_root = resolved_run_dir / "release"
    assessment_path = _write_json(release_root / "assessment.json", assessment)

    from .publish import publish_recovery_run

    publish_summary = publish_recovery_run(
        run_dir=resolved_run_dir,
        model_group=model_group,
        profile_id=profile_id,
        threshold_source=normalized_threshold_source,
        root=root,
        allow_unsafe_publish=allow_unsafe_publish,
    )

    runtime_env = {
        "STRATEGY_ENGINE": "ml_pure",
        "ML_PURE_RUN_ID": str(publish_summary["run_id"]),
        "ML_PURE_MODEL_GROUP": str(publish_summary["model_group"]),
    }
    runtime_env_path = _write_env(release_root / "ml_pure_runtime.env", runtime_env)
    gcs_sync: Optional[Dict[str, Any]] = None
    if model_bucket_url:
        gcs_sync = sync_published_model_group_to_gcs(
            model_bucket_url=str(model_bucket_url),
            model_group=str(publish_summary["model_group"]),
            root=root,
        )

    result = {
        "created_at_utc": utc_now(),
        "status": "completed",
        "run_dir": str(resolved_run_dir),
        "run_id": str(publish_summary["run_id"]),
        "model_group": str(publish_summary["model_group"]),
        "profile_id": str(publish_summary["profile_id"]),
        "threshold_source": normalized_threshold_source,
        "research_summary": research_summary,
        "threshold_sweep": threshold_sweep_payload,
        "assessment": assessment,
        "publish": publish_summary,
        "gcs_sync": gcs_sync,
        "live_handoff": {
            "engine": "ml_pure",
            "env": runtime_env,
        },
        "evaluation_handoff": {
            "engine": "ml_pure",
            "env": runtime_env,
            "note": "Use the same ML_PURE_* env for runtime or historical evaluation runs.",
        },
        "paths": {
            "assessment": str(assessment_path.resolve()),
            "runtime_env": str(runtime_env_path.resolve()),
        },
    }
    summary_path = _write_json(release_root / "release_summary.json", result)
    result["paths"]["release_summary"] = str(summary_path.resolve())
    return result
