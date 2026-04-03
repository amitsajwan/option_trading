from __future__ import annotations

import json
import math
import os
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..contracts.manifests import STAGED_GRID_KIND, manifest_hash
from ..experiment_control.coordination import (
    CoordinationError,
    RunReuseMode,
    acquire_directory_lock,
    prepare_output_root,
)
from ..experiment_control.registry import finalize_grid_status, initialize_grid_status
from ..experiment_control.runner import run_research
from ..experiment_control.state import utc_now
from .publish import release_staged_run
from .robustness import bootstrap_stage2_scores_from_parquet


def _timestamp_suffix() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _run_lane(resolved_config: Dict[str, Any], run_output_root: Path) -> Dict[str, Any]:
    return run_research(resolved_config, run_output_root=run_output_root, run_reuse_mode="fail_if_exists")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = deepcopy(value)
    return result


def _metric_float(value: Any, *, default: float) -> float:
    try:
        metric = float(value)
    except Exception:
        return float(default)
    return metric if math.isfinite(metric) else float(default)


def _ranking_tuple(row: Dict[str, Any]) -> tuple[float, ...]:
    stage2_cv = dict(row.get("stage2_cv") or {})
    combined = dict(row.get("combined_holdout_summary") or {})
    return (
        float(1 if bool(row.get("publishable")) else 0),
        float(1 if bool(stage2_cv.get("gate_passed")) else 0),
        _metric_float(stage2_cv.get("roc_auc"), default=float("-inf")),
        -_metric_float(stage2_cv.get("brier"), default=float("inf")),
        _metric_float(combined.get("profit_factor"), default=float("-inf")),
        _metric_float(combined.get("net_return_sum"), default=float("-inf")),
        -_metric_float(combined.get("max_drawdown_pct"), default=float("inf")),
    )


def _sort_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        [dict(row) for row in rows],
        key=lambda row: (_ranking_tuple(row), -int(row.get("sequence", 0))),
        reverse=True,
    )


def _best_prior_row(
    completed_rows: Dict[str, Dict[str, Any]],
    *,
    inherit_best_from: Sequence[str],
) -> Dict[str, Any]:
    candidate_rows = [
        dict(completed_rows[run_id])
        for run_id in inherit_best_from
        if run_id in completed_rows and str(completed_rows[run_id].get("release_status")) != "failed"
    ]
    if not candidate_rows:
        raise ValueError(f"inherit_best_from has no successful prior runs: {list(inherit_best_from)}")
    return _sort_rows(candidate_rows)[0]


def _resolve_run_overrides(
    *,
    run_spec: Dict[str, Any],
    completed_rows: Dict[str, Dict[str, Any]],
) -> tuple[Dict[str, Any], Optional[str]]:
    applied_overrides: Dict[str, Any] = {}
    inherited_from_run_id: Optional[str] = None
    inherit_best_from = list(run_spec.get("inherit_best_from") or [])
    if inherit_best_from:
        inherited_row = _best_prior_row(completed_rows, inherit_best_from=inherit_best_from)
        applied_overrides = _deep_merge(applied_overrides, dict(inherited_row.get("applied_overrides") or {}))
        inherited_from_run_id = str(inherited_row["grid_run_id"])
    applied_overrides = _deep_merge(applied_overrides, dict(run_spec.get("overrides") or {}))
    return applied_overrides, inherited_from_run_id


def _run_status_from_summary(summary: Dict[str, Any]) -> str:
    publishable = bool(((summary.get("publish_assessment") or {}).get("publishable")))
    return "publishable_candidate" if publishable else "held"


def _join_model_group(base_model_group: str, suffix: str) -> str:
    return f"{str(base_model_group).strip()}{str(suffix or '').strip()}"


def _existing_lane_row(
    *,
    sequence: int,
    run_spec: Dict[str, Any],
    manifest_path: Path,
    run_output_root: Path,
    model_group: str,
    profile_id: str,
    applied_overrides: Dict[str, Any],
    inherited_from_run_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    summary_path = run_output_root / "summary.json"
    if summary_path.exists():
        return _result_row(
            sequence=sequence,
            run_spec=run_spec,
            resolved_manifest_path=manifest_path,
            run_output_root=run_output_root,
            model_group=model_group,
            profile_id=profile_id,
            summary=_load_json(summary_path),
            applied_overrides=applied_overrides,
            inherited_from_run_id=inherited_from_run_id,
        )
    if run_output_root.exists() and any(run_output_root.iterdir()):
        raise CoordinationError(
            f"lane root contains partial artifacts without summary.json: {run_output_root}. "
            "Use restart or a fresh grid output root."
        )
    return None


def _result_row(
    *,
    sequence: int,
    run_spec: Dict[str, Any],
    resolved_manifest_path: Path,
    run_output_root: Path,
    model_group: str,
    profile_id: str,
    summary: Dict[str, Any],
    applied_overrides: Dict[str, Any],
    inherited_from_run_id: Optional[str],
) -> Dict[str, Any]:
    publish_assessment = dict(summary.get("publish_assessment") or {})
    stage1_cv = dict(summary.get("cv_prechecks", {}).get("stage1_cv") or {})
    stage2_cv = dict(summary.get("cv_prechecks", {}).get("stage2_cv") or {})
    combined_holdout = dict((((summary.get("holdout_reports") or {}).get("stage3") or {}).get("combined_holdout_summary")) or {})
    label_filtering = dict((summary.get("label_filtering") or {}).get("stage2") or {})
    stage2_artifacts = dict((summary.get("stage_artifacts") or {}).get("stage2") or {})
    blocking_reasons = list(publish_assessment.get("blocking_reasons") or [])
    return {
        "sequence": int(sequence),
        "grid_run_id": str(run_spec["run_id"]),
        "manifest_path": str(resolved_manifest_path.resolve()),
        "run_dir": str(run_output_root.resolve()),
        "summary_path": str((run_output_root / "summary.json").resolve()),
        "release_status": _run_status_from_summary(summary),
        "completion_mode": str(summary.get("completion_mode") or ""),
        "execution_integrity": str(summary.get("execution_integrity") or "unknown"),
        "publishable": bool(publish_assessment.get("publishable", False)),
        "publish_decision": str(publish_assessment.get("decision") or "HOLD"),
        "blocking_reasons": blocking_reasons,
        "dominant_blocking_reason": str(blocking_reasons[0]) if blocking_reasons else None,
        "model_group": str(model_group),
        "profile_id": str(profile_id),
        "stage1_cv": stage1_cv,
        "stage2_cv": stage2_cv,
        "combined_holdout_summary": combined_holdout,
        "scenario_reports": dict(summary.get("scenario_reports") or {}),
        "stage2_label_filtering": label_filtering,
        "stage2_diagnostics_path": stage2_artifacts.get("diagnostics_path"),
        "stage2_diagnostics_score_paths": dict(stage2_artifacts.get("diagnostics_score_paths") or {}),
        "applied_overrides": applied_overrides,
        "inherited_from_run_id": inherited_from_run_id,
    }


def _failed_result_row(
    *,
    sequence: int,
    run_spec: Dict[str, Any],
    manifest_path: Path,
    run_output_root: Path,
    model_group: str,
    profile_id: str,
    error: Exception,
    applied_overrides: Dict[str, Any],
    inherited_from_run_id: Optional[str],
) -> Dict[str, Any]:
    reason = f"{type(error).__name__}: {error}"
    return {
        "sequence": int(sequence),
        "grid_run_id": str(run_spec["run_id"]),
        "manifest_path": str(manifest_path.resolve()),
        "run_dir": str(run_output_root.resolve()),
        "summary_path": (
            str((run_output_root / "summary.json").resolve())
            if (run_output_root / "summary.json").exists()
            else None
        ),
        "release_status": "failed",
        "completion_mode": "failed",
        "execution_integrity": "contaminated",
        "publishable": False,
        "publish_decision": "HOLD",
        "blocking_reasons": [reason],
        "dominant_blocking_reason": reason,
        "model_group": str(model_group),
        "profile_id": str(profile_id),
        "stage1_cv": {},
        "stage2_cv": {},
        "combined_holdout_summary": {},
        "scenario_reports": {},
        "applied_overrides": applied_overrides,
        "inherited_from_run_id": inherited_from_run_id,
    }


def _dominant_failure_reason(rows: Sequence[Dict[str, Any]]) -> Optional[str]:
    reasons: Counter[str] = Counter()
    for row in rows:
        if bool(row.get("publishable")):
            continue
        for reason in list(row.get("blocking_reasons") or []):
            reasons[str(reason)] += 1
    if not reasons:
        return None
    return sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _stage2_hpo_escalation(rows: Sequence[Dict[str, Any]], thresholds: Dict[str, Any]) -> Dict[str, Any]:
    ranked = _sort_rows(rows)
    best_row = ranked[0] if ranked else None
    stage2_cv = dict((best_row or {}).get("stage2_cv") or {})
    roc_auc_min = _metric_float(thresholds.get("roc_auc_min"), default=float("nan"))
    brier_max = _metric_float(thresholds.get("brier_max"), default=float("nan"))
    roc_auc = _metric_float(stage2_cv.get("roc_auc"), default=float("-inf"))
    brier = _metric_float(stage2_cv.get("brier"), default=float("inf"))
    eligible = best_row is not None and roc_auc >= roc_auc_min and brier <= brier_max
    return {
        "thresholds": {
            "roc_auc_min": roc_auc_min,
            "brier_max": brier_max,
        },
        "best_run_id": None if best_row is None else str(best_row["grid_run_id"]),
        "best_stage2_cv": stage2_cv,
        "eligible": bool(eligible),
        "recommended_next_step": (
            "consider_stage2_hpo"
            if eligible
            else "move_to_stage2_label_or_view_redesign"
        ),
    }


def _normalize_robustness_probe(selection: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(selection.get("robustness_probe") or {})
    enabled = bool(raw.get("enabled", False))
    splits = [str(item).strip() for item in list(raw.get("splits") or ["research_valid", "final_holdout"]) if str(item).strip()]
    return {
        "enabled": enabled,
        "top_k": max(1, int(raw.get("top_k", 3))),
        "iterations": max(1, int(raw.get("iterations", 200))),
        "random_seed": max(1, int(raw.get("random_seed", 42))),
        "resample_unit": str(raw.get("resample_unit") or "trade_date").strip().lower(),
        "splits": splits,
    }


def _attach_stage2_robustness_probe(
    rows: List[Dict[str, Any]],
    *,
    selection: Dict[str, Any],
    stage2_gates: Dict[str, Any],
) -> Dict[str, Any]:
    probe = _normalize_robustness_probe(selection)
    if not probe["enabled"]:
        return {"enabled": False, "evaluated_run_ids": []}
    evaluated_run_ids: list[str] = []
    roc_auc_min = _metric_float(stage2_gates.get("roc_auc_min"), default=float("nan"))
    brier_max = _metric_float(stage2_gates.get("brier_max"), default=float("nan"))
    row_by_id = {str(row.get("grid_run_id")): row for row in rows}
    ranked_candidates = [
        row
        for row in _sort_rows(rows)
        if str(row.get("release_status")) != "failed"
    ][: int(probe["top_k"])]
    for ranked_row in ranked_candidates:
        row = row_by_id[str(ranked_row["grid_run_id"])]
        score_paths = dict(row.get("stage2_diagnostics_score_paths") or {})
        robustness: Dict[str, Any] = {
            "probe_config": dict(probe),
            "splits": {},
        }
        for split_name in list(probe["splits"]):
            score_path = score_paths.get(split_name)
            if not score_path:
                robustness["splits"][split_name] = {"status": "missing_score_path"}
                continue
            robustness["splits"][split_name] = {
                "status": "computed",
                **bootstrap_stage2_scores_from_parquet(
                    score_path,
                    iterations=int(probe["iterations"]),
                    random_seed=int(probe["random_seed"]),
                    roc_auc_min=roc_auc_min,
                    brier_max=brier_max,
                ),
            }
        row["stage2_robustness"] = robustness
        evaluated_run_ids.append(str(row["grid_run_id"]))
    return {
        **probe,
        "evaluated_run_ids": evaluated_run_ids,
    }


def _base_model_n_jobs(grid_resolved: Dict[str, Any]) -> int:
    base_manifest = dict(grid_resolved.get("base_resolved_manifest") or {})
    try:
        value = (((base_manifest.get("training") or {}).get("runtime") or {}).get("model_n_jobs", 1))
        return max(1, int(value))
    except Exception:
        return 1


def _effective_max_parallel_runs(grid_resolved: Dict[str, Any]) -> int:
    configured = grid_resolved.get("grid", {}).get("max_parallel_runs")
    if configured is not None:
        return max(1, int(configured))
    model_n_jobs = _base_model_n_jobs(grid_resolved)
    host_cores = max(1, int(os.cpu_count() or 1))
    return max(1, host_cores // model_n_jobs)


def _build_lane_resolved_config(
    *,
    base_resolved_manifest: Dict[str, Any],
    resolved_overrides: Dict[str, Any],
    merged_manifest: Dict[str, Any],
    manifest_path: Path,
) -> Dict[str, Any]:
    resolved_config = _deep_merge(base_resolved_manifest, resolved_overrides)
    if not str(((resolved_config.get("outputs") or {}).get("run_name")) or "").strip():
        resolved_config.setdefault("outputs", {})
        resolved_config["outputs"]["run_name"] = str(merged_manifest["outputs"]["run_name"])
    resolved_config["manifest_path"] = str(manifest_path.resolve())
    resolved_config["manifest_hash"] = manifest_hash(merged_manifest)
    resolved_config["raw_manifest"] = deepcopy(merged_manifest)
    return resolved_config


def run_staged_grid(
    grid_resolved: Dict[str, Any],
    *,
    model_group: str,
    profile_id: str,
    run_output_root: Optional[Path] = None,
    run_reuse_mode: RunReuseMode = "fail_if_exists",
    publish_winner: bool = False,
    model_bucket_url: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    if str(grid_resolved.get("experiment_kind") or "") != STAGED_GRID_KIND:
        raise ValueError(f"grid_resolved must be {STAGED_GRID_KIND}")
    if bool(grid_resolved.get("grid", {}).get("research_only", True)) and publish_winner:
        raise ValueError("grid manifest is research_only; rerun the selected winner through the normal release flow instead")

    base_resolved_manifest = dict(grid_resolved.get("base_resolved_manifest") or {})
    if not base_resolved_manifest:
        raise ValueError("grid_resolved is missing base_resolved_manifest")
    base_raw_manifest = dict(grid_resolved.get("base_raw_manifest") or {})
    if not base_raw_manifest:
        raise ValueError("grid_resolved is missing base_raw_manifest")

    requested_grid_root = (
        Path(run_output_root).resolve()
        if run_output_root is not None
        else Path(grid_resolved["outputs"]["artifacts_root"]) / f"{grid_resolved['outputs']['run_name']}_{_timestamp_suffix()}"
    )
    prep = prepare_output_root(
        requested_grid_root,
        reuse_mode=run_reuse_mode,
        summary_filename="grid_summary.json",
        entity_name="staged grid root",
        lock_filename=".grid.lock",
    )
    existing_summary = prep.get("existing_summary")
    if isinstance(existing_summary, dict):
        return existing_summary
    grid_root = Path(prep["output_root"]).resolve()
    archived_root = str(prep.get("archived_root") or "") or None
    manifests_root = grid_root / "manifests"
    runs_root = grid_root / "runs"
    grid_root.mkdir(parents=True, exist_ok=True)
    manifests_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)

    summary_path = (grid_root / "grid_summary.json").resolve()
    try:
        with acquire_directory_lock(
            grid_root,
            lock_filename=".grid.lock",
            entity_name="staged grid",
            manifest_hash=str(grid_resolved.get("manifest_hash", "")),
        ):
            max_parallel_runs = _effective_max_parallel_runs(grid_resolved)
            initialize_grid_status(
                grid_root=grid_root,
                grid_run_id=str(grid_root.name),
                manifest_hash=str(grid_resolved.get("manifest_hash", "")),
                run_reuse_mode=str(run_reuse_mode),
                archived_root=archived_root,
                max_parallel_runs=max_parallel_runs,
            )
            run_rows: List[Dict[str, Any]] = []
            completed_rows: Dict[str, Dict[str, Any]] = {}
            pending_specs = [
                {"sequence": int(sequence), "run_spec": dict(run_spec)}
                for sequence, run_spec in enumerate(list(grid_resolved["grid"]["runs"]), start=1)
            ]
            running: Dict[Future[Dict[str, Any]], Dict[str, Any]] = {}

            while pending_specs:
                resumed_any = False
                for item in list(pending_specs):
                    sequence = int(item["sequence"])
                    run_spec = dict(item["run_spec"])
                    if not all(str(ref) in completed_rows for ref in list(run_spec.get("inherit_best_from") or [])):
                        continue
                    candidate_model_group = _join_model_group(model_group, str(run_spec.get("model_group_suffix") or ""))
                    manifest_path = manifests_root / f"{sequence:02d}_{run_spec['run_id']}.json"
                    run_dir = runs_root / f"{sequence:02d}_{run_spec['run_id']}"
                    resolved_overrides, inherited_from_run_id = _resolve_run_overrides(
                        run_spec=run_spec,
                        completed_rows=completed_rows,
                    )
                    existing_row = _existing_lane_row(
                        sequence=sequence,
                        run_spec=run_spec,
                        manifest_path=manifest_path,
                        run_output_root=run_dir,
                        model_group=candidate_model_group,
                        profile_id=profile_id,
                        applied_overrides=resolved_overrides,
                        inherited_from_run_id=inherited_from_run_id,
                    )
                    if existing_row is None:
                        continue
                    pending_specs.remove(item)
                    run_rows.append(existing_row)
                    completed_rows[str(run_spec["run_id"])] = existing_row
                    resumed_any = True
                if not resumed_any:
                    break

            with ThreadPoolExecutor(max_workers=max_parallel_runs, thread_name_prefix="staged-grid") as executor:
                while pending_specs or running:
                    launched = False
                    while len(running) < max_parallel_runs:
                        ready_spec = next(
                            (
                                item
                                for item in pending_specs
                                if all(str(ref) in completed_rows for ref in list(item["run_spec"].get("inherit_best_from") or []))
                            ),
                            None,
                        )
                        if ready_spec is None:
                            break

                        pending_specs.remove(ready_spec)
                        sequence = int(ready_spec["sequence"])
                        run_spec = dict(ready_spec["run_spec"])
                        run_dir = runs_root / f"{sequence:02d}_{run_spec['run_id']}"
                        candidate_model_group = _join_model_group(model_group, str(run_spec.get("model_group_suffix") or ""))
                        manifest_path = manifests_root / f"{sequence:02d}_{run_spec['run_id']}.json"
                        try:
                            resolved_overrides, inherited_from_run_id = _resolve_run_overrides(
                                run_spec=run_spec,
                                completed_rows=completed_rows,
                            )
                            merged_manifest = _deep_merge(base_raw_manifest, resolved_overrides)
                            if not str(((merged_manifest.get("outputs") or {}).get("run_name")) or "").strip():
                                merged_manifest.setdefault("outputs", {})
                                merged_manifest["outputs"]["run_name"] = f"{base_raw_manifest['outputs']['run_name']}_{run_spec['run_id']}"
                            lane_resolved_config = _build_lane_resolved_config(
                                base_resolved_manifest=base_resolved_manifest,
                                resolved_overrides=resolved_overrides,
                                merged_manifest=merged_manifest,
                                manifest_path=manifest_path,
                            )
                            _write_json(manifest_path, merged_manifest)
                        except Exception as exc:
                            row = _failed_result_row(
                                sequence=sequence,
                                run_spec=run_spec,
                                manifest_path=manifest_path,
                                run_output_root=run_dir,
                                model_group=candidate_model_group,
                                profile_id=profile_id,
                                error=exc,
                                applied_overrides=dict(run_spec.get("overrides") or {}),
                                inherited_from_run_id=None,
                            )
                            run_rows.append(row)
                            completed_rows[str(run_spec["run_id"])] = row
                            continue

                        future = executor.submit(_run_lane, lane_resolved_config, run_dir)
                        running[future] = {
                            "sequence": sequence,
                            "run_spec": run_spec,
                            "manifest_path": manifest_path,
                            "run_output_root": run_dir,
                            "model_group": candidate_model_group,
                            "profile_id": profile_id,
                            "applied_overrides": resolved_overrides,
                            "inherited_from_run_id": inherited_from_run_id,
                        }
                        launched = True

                    if not running:
                        if pending_specs:
                            for item in pending_specs:
                                run_spec = dict(item["run_spec"])
                                manifest_path = manifests_root / f"{int(item['sequence']):02d}_{run_spec['run_id']}.json"
                                run_dir = runs_root / f"{int(item['sequence']):02d}_{run_spec['run_id']}"
                                row = _failed_result_row(
                                    sequence=int(item["sequence"]),
                                    run_spec=run_spec,
                                    manifest_path=manifest_path,
                                    run_output_root=run_dir,
                                    model_group=_join_model_group(model_group, str(run_spec.get("model_group_suffix") or "")),
                                    profile_id=profile_id,
                                    error=ValueError(
                                        "unresolved grid dependencies; check inherit_best_from ordering and references"
                                    ),
                                    applied_overrides=dict(run_spec.get("overrides") or {}),
                                    inherited_from_run_id=None,
                                )
                                run_rows.append(row)
                                completed_rows[str(run_spec["run_id"])] = row
                            pending_specs = []
                        break

                    if launched and len(running) < max_parallel_runs and pending_specs:
                        continue

                    done, _ = wait(tuple(running.keys()), return_when=FIRST_COMPLETED)
                    for future in done:
                        meta = running.pop(future)
                        try:
                            summary = future.result()
                            row = _result_row(
                                sequence=int(meta["sequence"]),
                                run_spec=dict(meta["run_spec"]),
                                resolved_manifest_path=Path(meta["manifest_path"]),
                                run_output_root=Path(meta["run_output_root"]),
                                model_group=str(meta["model_group"]),
                                profile_id=str(meta["profile_id"]),
                                summary=summary,
                                applied_overrides=dict(meta["applied_overrides"]),
                                inherited_from_run_id=meta["inherited_from_run_id"],
                            )
                        except Exception as exc:
                            row = _failed_result_row(
                                sequence=int(meta["sequence"]),
                                run_spec=dict(meta["run_spec"]),
                                manifest_path=Path(meta["manifest_path"]),
                                run_output_root=Path(meta["run_output_root"]),
                                model_group=str(meta["model_group"]),
                                profile_id=str(meta["profile_id"]),
                                error=exc,
                                applied_overrides=dict(meta["applied_overrides"]),
                                inherited_from_run_id=meta["inherited_from_run_id"],
                            )
                        run_rows.append(row)
                        completed_rows[str(meta["run_spec"]["run_id"])] = row

        run_rows = sorted(run_rows, key=lambda row: int(row.get("sequence", 0)))
        ranked_rows = _sort_rows(run_rows)
        for rank, row in enumerate(ranked_rows, start=1):
            row["rank"] = int(rank)
        rank_by_run_id = {str(row["grid_run_id"]): int(row["rank"]) for row in ranked_rows}
        for row in run_rows:
            row["rank"] = rank_by_run_id[str(row["grid_run_id"])]

        robustness_probe = _attach_stage2_robustness_probe(
            run_rows,
            selection=dict(grid_resolved.get("selection") or {}),
            stage2_gates=dict((base_resolved_manifest.get("hard_gates") or {}).get("stage2") or {}),
        )
        ranked_rows = _sort_rows(run_rows)
        for rank, row in enumerate(ranked_rows, start=1):
            row["rank"] = int(rank)
        rank_by_run_id = {str(row["grid_run_id"]): int(row["rank"]) for row in ranked_rows}
        for row in run_rows:
            row["rank"] = rank_by_run_id[str(row["grid_run_id"])]

        winner = dict(ranked_rows[0]) if ranked_rows else None
        any_failed = any(str(row.get("release_status")) == "failed" for row in run_rows)
        stage2_hpo_escalation = _stage2_hpo_escalation(
            ranked_rows,
            dict(grid_resolved["selection"]["stage2_hpo_escalation"]),
        )
        dominant_failure_reason = None if winner and bool(winner.get("publishable")) else _dominant_failure_reason(ranked_rows)

        winner_release = None
        if publish_winner and winner and bool(winner.get("publishable")):
            winner_release = release_staged_run(
                run_dir=Path(winner["run_dir"]),
                model_group=str(winner["model_group"]),
                profile_id=str(profile_id),
                model_bucket_url=model_bucket_url,
                root=root,
            )

        payload: Dict[str, Any] = {
            "created_at_utc": utc_now(),
            "status": "completed_with_failures" if any_failed else "completed",
            "experiment_kind": STAGED_GRID_KIND,
            "grid_run_id": str(grid_root.name),
            "orchestration_integrity": "clean",
            "grid_manifest_path": str(Path(grid_resolved["manifest_path"]).resolve()),
            "base_manifest_path": str(Path(grid_resolved["inputs"]["base_manifest_path"]).resolve()),
            "research_only": bool(grid_resolved.get("grid", {}).get("research_only", True)),
            "model_group": str(model_group),
            "profile_id": str(profile_id),
            "execution": {
                "max_parallel_runs": int(max_parallel_runs),
                "host_cpu_count": max(1, int(os.cpu_count() or 1)),
                "base_model_n_jobs": _base_model_n_jobs(grid_resolved),
                "run_reuse_mode": str(run_reuse_mode),
            },
            "runs": run_rows,
            "ranking": [
                {
                    "rank": int(row["rank"]),
                    "grid_run_id": str(row["grid_run_id"]),
                    "publishable": bool(row["publishable"]),
                    "release_status": str(row["release_status"]),
                }
                for row in ranked_rows
            ],
            "winner": winner,
            "winner_release": winner_release,
            "dominant_failure_reason": dominant_failure_reason,
            "stage2_hpo_escalation": stage2_hpo_escalation,
            "robustness_probe": robustness_probe,
            "paths": {
                "grid_root": str(grid_root.resolve()),
                "manifests_root": str(manifests_root.resolve()),
                "runs_root": str(runs_root.resolve()),
                "grid_summary": str(summary_path),
                "grid_status": str((grid_root / "grid_status.json").resolve()),
                "archived_root": prep.get("archived_root"),
            },
        }
        finalize_grid_status(
            grid_root=grid_root,
            grid_run_id=str(grid_root.name),
            manifest_hash=str(grid_resolved.get("manifest_hash", "")),
            run_reuse_mode=str(run_reuse_mode),
            archived_root=archived_root,
            lifecycle_status=("failed" if payload["status"] == "failed" else "completed"),
            dominant_failure_reason=dominant_failure_reason,
            winner_run_id=(None if winner is None else str(winner.get("grid_run_id"))),
        )
        _write_json(summary_path, payload)
        return payload
    except Exception as exc:
        payload = {
            "created_at_utc": utc_now(),
            "status": "failed",
            "experiment_kind": STAGED_GRID_KIND,
            "grid_run_id": str(grid_root.name),
            "orchestration_integrity": "contaminated",
            "grid_manifest_path": str(Path(grid_resolved["manifest_path"]).resolve()),
            "base_manifest_path": str(Path(grid_resolved["inputs"]["base_manifest_path"]).resolve()),
            "research_only": bool(grid_resolved.get("grid", {}).get("research_only", True)),
            "model_group": str(model_group),
            "profile_id": str(profile_id),
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
            "paths": {
                "grid_root": str(grid_root.resolve()),
                "manifests_root": str(manifests_root.resolve()),
                "runs_root": str(runs_root.resolve()),
                "grid_summary": str(summary_path),
                "grid_status": str((grid_root / "grid_status.json").resolve()),
                "archived_root": prep.get("archived_root"),
            },
        }
        finalize_grid_status(
            grid_root=grid_root,
            grid_run_id=str(grid_root.name),
            manifest_hash=str(grid_resolved.get("manifest_hash", "")),
            run_reuse_mode=str(run_reuse_mode),
            archived_root=archived_root,
            lifecycle_status="failed",
            dominant_failure_reason=str(exc),
            winner_run_id=None,
        )
        _write_json(summary_path, payload)
        return payload


__all__ = ["run_staged_grid"]
