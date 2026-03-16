from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from .catalog.feature_sets import feature_set_names
from .catalog.models import model_names
from .contracts.manifests import RECOVERY_KIND, load_and_resolve_manifest
from .experiment_control.background import get_background_job_status, launch_background_job


DEFAULT_BASE_MANIFEST = "ml_pipeline_2/configs/research/fo_expiry_aware_recovery.default.json"
DEFAULT_TP_GRID = (0.0020, 0.0025, 0.0030)
DEFAULT_SL_GRID = (0.0008, 0.0010, 0.0012)
DEFAULT_HORIZON_GRID = (15, 20)
DEFAULT_BARRIER_MODES = ("fixed", "atr_scaled")
DEFAULT_MODELS = ("xgb_shallow", "lgbm_dart", "logreg_balanced")
DEFAULT_FEATURE_SETS = ("fo_expiry_aware_v2", "fo_oi_pcr_momentum", "fo_no_time_context")
DEFAULT_MATRIX_ROOT = "ml_pipeline_2/artifacts/research_matrices"
DEFAULT_JOB_ROOT = "ml_pipeline_2/artifacts/background_jobs"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_suffix() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _sanitize_name(value: object) -> str:
    cleaned = "".join(ch.lower() if str(ch).isalnum() else "_" for ch in str(value))
    collapsed = "_".join(part for part in cleaned.split("_") if part)
    return collapsed or "item"


def _parse_float_grid(value: Any, default_items: Sequence[float]) -> List[float]:
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]
    if value is None:
        return [float(item) for item in default_items]
    return [float(part) for part in str(value).split(",") if str(part).strip()]


def _parse_int_grid(value: Any, default_items: Sequence[int]) -> List[int]:
    if isinstance(value, (list, tuple)):
        return [int(item) for item in value]
    if value is None:
        return [int(item) for item in default_items]
    return [int(part) for part in str(value).split(",") if str(part).strip()]


def _parse_name_list(value: Any, default_items: Sequence[str]) -> List[str]:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or [str(item) for item in default_items]
    if value is None:
        return [str(item) for item in default_items]
    items = [part.strip() for part in str(value).split(",") if part.strip()]
    return items or [str(item) for item in default_items]


def _parse_recipe_list(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("matrix.recipes must be a list of recipe objects")
    recipes: List[Dict[str, Any]] = []
    for idx, item in enumerate(list(value)):
        if not isinstance(item, dict):
            raise ValueError(f"matrix.recipes[{idx}] must be an object")
        recipe_id = str(item.get("recipe_id") or "").strip()
        barrier_mode = str(item.get("barrier_mode") or "").strip()
        if not recipe_id:
            raise ValueError(f"matrix.recipes[{idx}].recipe_id must not be empty")
        if not barrier_mode:
            raise ValueError(f"matrix.recipes[{idx}].barrier_mode must not be empty")
        recipes.append(
            {
                "recipe_id": recipe_id,
                "horizon_minutes": int(item.get("horizon_minutes")),
                "take_profit_pct": float(item.get("take_profit_pct")),
                "stop_loss_pct": float(item.get("stop_loss_pct")),
                "barrier_mode": barrier_mode,
            }
        )
    return recipes


def _load_config(path: Optional[str]) -> tuple[Dict[str, Any], Path]:
    if path is None or not str(path).strip():
        return {}, Path.cwd().resolve()
    config_path = Path(path).resolve()
    return _read_json(config_path), config_path.parent


def _pick(cli_value: Any, config_value: Any, default_value: Any) -> Any:
    return cli_value if cli_value is not None else (config_value if config_value is not None else default_value)


def _resolve_abs_path(value: Any, *, base_dir: Path) -> Path:
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _resolve_path_choice(cli_value: Any, config_value: Any, default_value: Any, *, config_dir: Path) -> str:
    if cli_value is not None:
        return str(_resolve_abs_path(cli_value, base_dir=Path.cwd().resolve()))
    if config_value is not None:
        return str(_resolve_abs_path(config_value, base_dir=config_dir))
    return str(_resolve_abs_path(default_value, base_dir=Path.cwd().resolve()))


def build_recovery_recipe_grid(
    *,
    horizon_grid: Sequence[int],
    tp_grid: Sequence[float],
    sl_grid: Sequence[float],
    barrier_modes: Sequence[str],
) -> List[Dict[str, Any]]:
    recipes: List[Dict[str, Any]] = []
    for horizon in list(horizon_grid):
        for take_profit in list(tp_grid):
            for stop_loss in list(sl_grid):
                for barrier_mode in list(barrier_modes):
                    mode_key = "ATR" if str(barrier_mode).strip().lower() == "atr_scaled" else "FIXED"
                    recipe_id = f"{mode_key}_H{int(horizon)}_TP{int(round(float(take_profit) * 10000.0))}_SL{int(round(float(stop_loss) * 10000.0))}"
                    recipes.append(
                        {
                            "recipe_id": recipe_id,
                            "horizon_minutes": int(horizon),
                            "take_profit_pct": float(take_profit),
                            "stop_loss_pct": float(stop_loss),
                            "barrier_mode": str(barrier_mode),
                        }
                    )
    return recipes


def _matrix_index_path(matrix_root: Path) -> Path:
    return matrix_root / "matrix.json"


def _combo_output_root(*, matrix_root: Path, combo_key: str) -> Path:
    return matrix_root / "runs" / combo_key


def _combo_job_metadata(*, matrix_root: Path, combo_key: str, feature_set: str, model_name: str, artifacts_root: Path, manifest_path: Path, recipe_id: Optional[str] = None, output_root: Optional[Path] = None) -> Dict[str, Any]:
    payload = {
        "matrix_root": str(matrix_root.resolve()),
        "combo_key": combo_key,
        "feature_set": str(feature_set),
        "primary_model": str(model_name),
        "recipe_id": (str(recipe_id).strip() or None) if recipe_id is not None else None,
        "summary_filename": "summary.json",
        "outputs": {
            "artifacts_root": str(artifacts_root.resolve()),
            "run_name": "run",
        },
        "manifest_path": str(manifest_path.resolve()),
        "experiment_kind": RECOVERY_KIND,
    }
    if output_root is not None:
        payload["output_root"] = str(output_root.resolve())
    return payload


def _latest_run_dir(artifacts_root: Path, *, run_name: str) -> Optional[Path]:
    if not artifacts_root.exists():
        return None
    candidates = sorted(
        [path for path in artifacts_root.glob(f"{run_name}_*") if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _combo_summary_path(artifacts_root: Path, *, run_name: str) -> Optional[Path]:
    latest_run_dir = _latest_run_dir(artifacts_root, run_name=run_name)
    if latest_run_dir is None:
        return None
    summary_path = latest_run_dir / "summary.json"
    return summary_path if summary_path.exists() else None


def _load_matrix_index(matrix_root: Path) -> Dict[str, Any]:
    return _read_json(_matrix_index_path(matrix_root))


def _validate_search_space_names(values: Sequence[str], *, kind: str, valid_options: Sequence[str]) -> None:
    selected = [str(value).strip() for value in list(values) if str(value).strip()]
    if not selected:
        raise ValueError(f"{kind} must not be empty")
    unknown = sorted(set(selected) - set(valid_options))
    if unknown:
        raise ValueError(f"unknown {kind}: {unknown}; valid options: {sorted(valid_options)}")


def generate_recovery_matrix(
    *,
    base_manifest_path: Path,
    matrix_root: Path,
    horizon_grid: Sequence[int],
    tp_grid: Sequence[float],
    sl_grid: Sequence[float],
    barrier_modes: Sequence[str],
    recipes_override: Optional[Sequence[Dict[str, Any]]],
    models: Sequence[str],
    feature_sets: Sequence[str],
    recipe_fanout: bool,
    launch_background: bool,
    job_root: Optional[Path],
    max_parallel_launches: Optional[int] = None,
) -> Dict[str, Any]:
    resolved = load_and_resolve_manifest(base_manifest_path, validate_paths=True)
    if str(resolved["experiment_kind"]) != RECOVERY_KIND:
        raise ValueError(f"base manifest must be {RECOVERY_KIND}: {base_manifest_path}")
    _validate_search_space_names(models, kind="models", valid_options=model_names())
    _validate_search_space_names(feature_sets, kind="feature_sets", valid_options=feature_set_names())
    recipes = (
        [dict(recipe) for recipe in list(recipes_override or [])]
        if recipes_override
        else build_recovery_recipe_grid(
            horizon_grid=horizon_grid,
            tp_grid=tp_grid,
            sl_grid=sl_grid,
            barrier_modes=barrier_modes,
        )
    )
    base_inputs = dict(resolved["inputs"])
    base_windows = json.loads(json.dumps(resolved["windows"], default=str))
    base_training = json.loads(json.dumps(resolved["training"], default=str))
    base_scenario = json.loads(json.dumps(resolved["scenario"], default=str))
    base_catalog = json.loads(json.dumps(resolved["catalog"], default=str))

    matrix_root.mkdir(parents=True, exist_ok=True)
    manifests_root = matrix_root / "manifests"
    combos: List[Dict[str, Any]] = []
    launch_cap = (max(0, int(max_parallel_launches)) if max_parallel_launches is not None else None)
    launched_count = 0
    for feature_set in list(feature_sets):
        for model_name in list(models):
            recipe_variants = list(recipes) if bool(recipe_fanout) else [None]
            for recipe in recipe_variants:
                combo_name = f"{feature_set}__{model_name}"
                scenario_payload = {
                    **dict(base_scenario),
                    "recipes": ([dict(recipe)] if recipe is not None else list(recipes)),
                    "primary_model": str(model_name),
                }
                combo_payload: Dict[str, Any] = {
                    "feature_set": str(feature_set),
                    "primary_model": str(model_name),
                    "run_name": "run",
                }
                if recipe is not None:
                    recipe_id = str(recipe.get("recipe_id") or "").strip()
                    combo_name = f"{combo_name}__{recipe_id}"
                    scenario_payload["recipe_selection"] = [recipe_id]
                    combo_payload["recipe_id"] = recipe_id
                combo_key = _sanitize_name(combo_name)
                artifacts_root = _combo_output_root(matrix_root=matrix_root, combo_key=combo_key)
                manifest_payload = {
                    "schema_version": int(resolved["schema_version"]),
                    "experiment_kind": str(resolved["experiment_kind"]),
                    "inputs": {
                        "model_window_features_path": str(Path(base_inputs["model_window_features_path"]).resolve()),
                        "holdout_features_path": str(Path(base_inputs["holdout_features_path"]).resolve()),
                        "base_path": str(Path(base_inputs["base_path"]).resolve()),
                        "baseline_json_path": (str(Path(base_inputs["baseline_json_path"]).resolve()) if base_inputs.get("baseline_json_path") is not None else ""),
                    },
                    "outputs": {
                        "artifacts_root": str(artifacts_root.resolve()),
                        "run_name": "run",
                    },
                    "catalog": {
                        "feature_profile": str(base_catalog["feature_profile"]),
                        "feature_sets": [str(feature_set)],
                        "models": [str(model_name)],
                    },
                    "windows": dict(base_windows),
                    "training": dict(base_training),
                    "scenario": scenario_payload,
                }
                manifest_path = manifests_root / f"{combo_key}.json"
                _write_json(manifest_path, manifest_payload)
                combo_payload.update(
                    {
                        "combo_key": combo_key,
                        "manifest_path": str(manifest_path.resolve()),
                        "artifacts_root": str(artifacts_root.resolve()),
                    }
                )
                should_launch = bool(launch_background) and (launch_cap is None or launched_count < launch_cap)
                if should_launch:
                    job = _launch_combo_job(
                        combo=combo_payload,
                        matrix_root=matrix_root,
                        job_root=job_root,
                    )
                    combo_payload["background_job_id"] = str(job["job_id"])
                    combo_payload["background_job_path"] = str((Path(job["job_dir"]) / "job.json").resolve())
                    launched_count += 1
                combos.append(combo_payload)
    index_payload = {
        "created_at_utc": _utc_now(),
        "matrix_root": str(matrix_root.resolve()),
        "base_manifest_path": str(base_manifest_path.resolve()),
        "recipe_count": int(len(recipes)),
        "recipe_fanout": bool(recipe_fanout),
        "max_parallel_launches": launch_cap,
        "recipes": list(recipes),
        "combos": combos,
    }
    _write_json(_matrix_index_path(matrix_root), index_payload)
    refresh_recovery_matrix_report(matrix_root)
    return index_payload


def _recipe_id(recipe: Dict[str, Any]) -> Optional[str]:
    if not isinstance(recipe, dict):
        return None
    return str(recipe.get("recipe_id") or "").strip() or None


def _candidate_rank_key(row: Dict[str, Any]) -> tuple[float, ...]:
    return (
        float(int(bool(row.get("effective_stage_a_passed", False)))),
        float(int(bool(row.get("effective_side_share_in_band", False)))),
        float(row.get("effective_profit_factor", float("-inf"))),
        float(row.get("effective_net_return_sum", float("-inf"))),
        float(row.get("effective_trades", 0.0)),
    )


def _report_status_counts(report: Dict[str, Any]) -> Dict[str, int]:
    rows = list(report.get("combos") or [])
    counts = {
        "completed": 0,
        "running": 0,
        "pending": 0,
        "failed": 0,
    }
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        if status in counts:
            counts[status] += 1
    return counts


def _combo_recipe_total(combo: Dict[str, Any]) -> Optional[int]:
    manifest_path = str(combo.get("manifest_path") or "").strip()
    if not manifest_path:
        return None
    payload = _read_json(Path(manifest_path).resolve())
    scenario = dict(payload.get("scenario") or {})
    recipes = list(scenario.get("recipes") or [])
    selected_recipe_ids = {
        str(item).strip()
        for item in list(scenario.get("recipe_selection") or [])
        if str(item).strip()
    }
    if not selected_recipe_ids:
        return int(len(recipes))
    return int(sum(1 for recipe in recipes if str((recipe or {}).get("recipe_id") or "").strip() in selected_recipe_ids))


def _completed_recipe_count(output_root: Optional[str]) -> int:
    if not output_root:
        return 0
    recipe_root = Path(output_root).resolve() / "primary_recipes"
    if not recipe_root.exists():
        return 0
    return int(sum(1 for path in recipe_root.glob("*/summary.json") if path.is_file()))


def _latest_state_info(output_root: Optional[str]) -> Dict[str, Any]:
    if not output_root:
        return {"last_state_event": None, "last_event_ts": None, "current_recipe_id": None}
    state_path = Path(output_root).resolve() / "state.jsonl"
    if not state_path.exists():
        return {"last_state_event": None, "last_event_ts": None, "current_recipe_id": None}
    last_event = None
    last_ts = None
    current_recipe_id = None
    for line in state_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        event = str(payload.get("event") or "").strip() or None
        recipe_id = str(payload.get("recipe_id") or "").strip() or None
        last_event = event
        last_ts = payload.get("ts_utc")
        if event == "primary_recipe_start":
            current_recipe_id = recipe_id
        elif event in {"primary_recipe_done", "primary_recipe_skipped"} and recipe_id and recipe_id == current_recipe_id:
            current_recipe_id = None
    return {
        "last_state_event": last_event,
        "last_event_ts": last_ts,
        "current_recipe_id": current_recipe_id,
    }


def _launch_combo_job(*, combo: Dict[str, Any], matrix_root: Path, job_root: Optional[Path], reuse_run_dir: Optional[Path] = None) -> Dict[str, Any]:
    manifest_path = Path(combo["manifest_path"]).resolve()
    artifacts_root = Path(combo["artifacts_root"]).resolve()
    args = ["--config", str(manifest_path)]
    if reuse_run_dir is not None:
        args.extend(["--run-output-root", str(reuse_run_dir.resolve())])
    job = launch_background_job(
        module="ml_pipeline_2.run_research",
        args=args,
        job_name=str(combo["combo_key"]),
        metadata=_combo_job_metadata(
            matrix_root=matrix_root,
            combo_key=str(combo["combo_key"]),
            feature_set=str(combo["feature_set"]),
            model_name=str(combo["primary_model"]),
            artifacts_root=artifacts_root,
            manifest_path=manifest_path,
            recipe_id=combo.get("recipe_id"),
            output_root=reuse_run_dir,
        ),
        job_root=job_root,
    )
    combo["background_job_id"] = str(job["job_id"])
    combo["background_job_path"] = str((Path(job["job_dir"]) / "job.json").resolve())
    return job


def launch_pending_recovery_matrix_jobs(matrix_root: Path, *, max_parallel: int, job_root: Optional[Path], retry_failed: bool = False) -> Dict[str, Any]:
    if int(max_parallel) <= 0:
        raise ValueError("max_parallel must be > 0")
    index_payload = _load_matrix_index(matrix_root)
    combos = list(index_payload.get("combos") or [])
    running_count = 0
    launched: List[str] = []
    for combo in combos:
        job_path = combo.get("background_job_path")
        if job_path:
            job_status = get_background_job_status(job_path=str(job_path))
            status = str(job_status.get("status"))
            if status == "running":
                running_count += 1
                continue
            if status == "completed" or not bool(retry_failed):
                continue
        summary_file = _combo_summary_path(Path(combo["artifacts_root"]).resolve(), run_name=str(combo["run_name"]))
        if summary_file is not None:
            continue
        if running_count >= int(max_parallel):
            continue
        reuse_run_dir = _latest_run_dir(Path(combo["artifacts_root"]).resolve(), run_name=str(combo["run_name"])) if job_path and bool(retry_failed) else None
        job = _launch_combo_job(
            combo=combo,
            matrix_root=matrix_root,
            job_root=job_root,
            reuse_run_dir=reuse_run_dir,
        )
        combo["background_job_id"] = str(job["job_id"])
        combo["background_job_path"] = str((Path(job["job_dir"]) / "job.json").resolve())
        launched.append(str(combo["combo_key"]))
        running_count += 1
    index_payload["combos"] = combos
    index_payload["max_parallel_launches"] = int(max_parallel)
    _write_json(_matrix_index_path(matrix_root), index_payload)
    report = refresh_recovery_matrix_report(matrix_root)
    return {
        "matrix_root": str(matrix_root.resolve()),
        "max_parallel": int(max_parallel),
        "retry_failed": bool(retry_failed),
        "running_count": int(sum(1 for row in report.get("combos", []) if str(row.get("status")) == "running")),
        "launched_combo_keys": launched,
        "report": report,
    }


def watch_pending_recovery_matrix_jobs(
    matrix_root: Path,
    *,
    max_parallel: int,
    job_root: Optional[Path],
    retry_failed: bool = False,
    poll_seconds: int = 120,
) -> Dict[str, Any]:
    if int(max_parallel) <= 0:
        raise ValueError("max_parallel must be > 0")
    if int(poll_seconds) <= 0:
        raise ValueError("poll_seconds must be > 0")
    iterations = 0
    launched_combo_keys: List[str] = []
    while True:
        payload = launch_pending_recovery_matrix_jobs(
            matrix_root,
            max_parallel=int(max_parallel),
            job_root=job_root,
            retry_failed=bool(retry_failed),
        )
        iterations += 1
        launched_combo_keys.extend(list(payload.get("launched_combo_keys") or []))
        report = dict(payload.get("report") or {})
        status_counts = _report_status_counts(report)
        if status_counts["pending"] == 0 and status_counts["running"] == 0:
            return {
                "matrix_root": str(matrix_root.resolve()),
                "max_parallel": int(max_parallel),
                "retry_failed": bool(retry_failed),
                "poll_seconds": int(poll_seconds),
                "iterations": int(iterations),
                "launched_combo_keys": launched_combo_keys,
                "status_counts": status_counts,
                "report": report,
            }
        time.sleep(int(poll_seconds))


def refresh_recovery_matrix_report(matrix_root: Path) -> Dict[str, Any]:
    index_payload = _load_matrix_index(matrix_root)
    summary_rows: List[Dict[str, Any]] = []
    recipe_rows: List[Dict[str, Any]] = []
    for combo in list(index_payload.get("combos") or []):
        job_path = combo.get("background_job_path")
        if job_path:
            job_status = get_background_job_status(job_path=str(job_path))
            status = str(job_status.get("status"))
            output_root = job_status.get("output_root")
            summary_path = job_status.get("summary_path")
        else:
            artifacts_root = Path(combo["artifacts_root"]).resolve()
            summary_file = _combo_summary_path(artifacts_root, run_name=str(combo["run_name"]))
            latest_run_dir = _latest_run_dir(artifacts_root, run_name=str(combo["run_name"]))
            output_root = str((summary_file.parent if summary_file is not None else latest_run_dir).resolve()) if (summary_file is not None or latest_run_dir is not None) else None
            summary_path = str(summary_file.resolve()) if summary_file is not None else None
            status = "completed" if summary_file is not None else "pending"
        recipe_total = _combo_recipe_total(combo)
        recipes_completed = _completed_recipe_count(output_root)
        state_info = _latest_state_info(output_root)
        summary_payload = _read_json(Path(summary_path)) if summary_path else {}
        primary_rows = list(summary_payload.get("primary_recipes") or [])
        selected_primary_recipe_id = str(summary_payload.get("selected_primary_recipe_id") or "").strip() or None
        selected_primary = next(
            (
                row
                for row in primary_rows
                if _recipe_id(dict(row.get("recipe") or {})) == selected_primary_recipe_id
            ),
            None,
        )
        primary_holdout = dict((selected_primary or {}).get("holdout_summary") or {})
        meta_gate = dict(summary_payload.get("meta_gate") or {})
        meta_holdout = dict(meta_gate.get("holdout_summary") or {})
        effective = meta_holdout if meta_holdout else primary_holdout
        summary_rows.append(
            {
                "combo_key": combo["combo_key"],
                "feature_set": combo["feature_set"],
                "primary_model": combo["primary_model"],
                "recipe_id": combo.get("recipe_id"),
                "status": status,
                "output_root": output_root,
                "summary_path": summary_path,
                "recipes_completed": int(recipes_completed),
                "recipes_total": recipe_total,
                "last_state_event": state_info["last_state_event"],
                "last_event_ts": state_info["last_event_ts"],
                "current_recipe_id": state_info["current_recipe_id"],
                "selected_primary_recipe_id": selected_primary_recipe_id,
                "primary_stage_a_passed": primary_holdout.get("stage_a_passed"),
                "primary_side_share_in_band": primary_holdout.get("side_share_in_band"),
                "primary_profit_factor": primary_holdout.get("profit_factor"),
                "primary_gross_profit_factor": primary_holdout.get("gross_profit_factor"),
                "primary_net_return_sum": primary_holdout.get("net_return_sum"),
                "primary_gross_return_sum": primary_holdout.get("gross_return_sum"),
                "primary_long_share": primary_holdout.get("long_share"),
                "primary_trades": primary_holdout.get("trades"),
                "primary_time_stop_net_wins": primary_holdout.get("time_stop_net_wins"),
                "primary_time_stop_net_losses": primary_holdout.get("time_stop_net_losses"),
                "meta_enabled": bool(meta_holdout),
                "meta_profit_factor": meta_holdout.get("profit_factor"),
                "meta_net_return_sum": meta_holdout.get("net_return_sum"),
                "meta_ce_share": meta_holdout.get("ce_share"),
                "meta_trades": meta_holdout.get("trades"),
                "effective_stage_a_passed": effective.get("stage_a_passed"),
                "effective_side_share_in_band": effective.get("side_share_in_band"),
                "effective_profit_factor": effective.get("profit_factor"),
                "effective_net_return_sum": effective.get("net_return_sum"),
                "effective_trades": effective.get("trades"),
            }
        )
        for primary in primary_rows:
            recipe = dict(primary.get("recipe") or {})
            holdout = dict(primary.get("holdout_summary") or {})
            recipe_rows.append(
                {
                    "combo_key": combo["combo_key"],
                    "feature_set": combo["feature_set"],
                    "primary_model": combo["primary_model"],
                    "recipe_id": recipe.get("recipe_id"),
                    "barrier_mode": recipe.get("barrier_mode"),
                    "horizon_minutes": recipe.get("horizon_minutes"),
                    "take_profit_pct": recipe.get("take_profit_pct"),
                    "stop_loss_pct": recipe.get("stop_loss_pct"),
                    "stage_a_passed": holdout.get("stage_a_passed"),
                    "side_share_in_band": holdout.get("side_share_in_band"),
                    "profit_factor": holdout.get("profit_factor"),
                    "gross_profit_factor": holdout.get("gross_profit_factor"),
                    "net_return_sum": holdout.get("net_return_sum"),
                    "gross_return_sum": holdout.get("gross_return_sum"),
                    "long_share": holdout.get("long_share"),
                    "trades": holdout.get("trades"),
                    "tp_trades": holdout.get("tp_trades"),
                    "sl_trades": holdout.get("sl_trades"),
                    "time_stop_trades": holdout.get("time_stop_trades"),
                    "invalid_trades": holdout.get("invalid_trades"),
                    "time_stop_gross_wins": holdout.get("time_stop_gross_wins"),
                    "time_stop_gross_losses": holdout.get("time_stop_gross_losses"),
                    "time_stop_net_wins": holdout.get("time_stop_net_wins"),
                    "time_stop_net_losses": holdout.get("time_stop_net_losses"),
                }
            )
    completed_rows = [row for row in summary_rows if str(row.get("status")) == "completed"]
    recommended = max(completed_rows, key=_candidate_rank_key) if completed_rows else None
    report = {
        "created_at_utc": _utc_now(),
        "matrix_root": str(matrix_root.resolve()),
        "combo_count": int(len(summary_rows)),
        "completed_count": int(len(completed_rows)),
        "recommended_combo_key": (recommended or {}).get("combo_key"),
        "recommended": recommended,
        "combos": summary_rows,
    }
    _write_json(matrix_root / "report.json", report)
    pd.DataFrame(summary_rows).to_csv(matrix_root / "report.csv", index=False)
    pd.DataFrame(recipe_rows).to_csv(matrix_root / "recipe_report.csv", index=False)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate, launch, and report recovery experiment matrices.")
    parser.add_argument("--config", help="Optional matrix config JSON")
    parser.add_argument("--base-manifest", help="Base recovery manifest JSON")
    parser.add_argument("--tp-grid", help="Comma-separated take-profit percents")
    parser.add_argument("--sl-grid", help="Comma-separated stop-loss percents")
    parser.add_argument("--horizon-grid", help="Comma-separated horizon minutes")
    parser.add_argument("--barrier-modes", help="Comma-separated barrier modes")
    parser.add_argument("--models", help="Comma-separated primary models")
    parser.add_argument("--feature-sets", help="Comma-separated feature sets")
    parser.add_argument("--recipe-fanout", action="store_true", help="Expand one combo per recipe so recipes can run independently")
    parser.add_argument("--matrix-root", help="Root directory for matrix artifacts")
    parser.add_argument("--matrix-name", help="Matrix run name prefix")
    parser.add_argument("--job-root", help="Background job registry root")
    parser.add_argument("--launch-background", action="store_true", help="Launch each combo as a detached background job")
    parser.add_argument("--launch-pending", action="store_true", help="Launch pending combos for an existing matrix root up to the parallel cap")
    parser.add_argument("--watch-pending", action="store_true", help="Continuously refill pending combos for an existing matrix root until all combos are completed or failed")
    parser.add_argument("--retry-failed", action="store_true", help="When launching pending jobs, also relaunch failed combos into their latest run directory")
    parser.add_argument("--max-parallel", type=int, help="Maximum number of background combos to keep active at once")
    parser.add_argument("--poll-seconds", type=int, help="Polling interval in seconds for --watch-pending")
    parser.add_argument("--report-only", action="store_true", help="Only refresh reports for an existing matrix root")
    return parser


def _resolve_args(args: argparse.Namespace) -> Dict[str, Any]:
    payload, config_dir = _load_config(args.config)
    inputs_cfg = dict(payload.get("inputs") or {})
    matrix_cfg = dict(payload.get("matrix") or {})
    outputs_cfg = dict(payload.get("outputs") or {})
    launch_cfg = dict(payload.get("launch") or {})
    base_manifest = _resolve_path_choice(args.base_manifest, inputs_cfg.get("base_manifest"), DEFAULT_BASE_MANIFEST, config_dir=config_dir)
    matrix_root_base = _resolve_path_choice(args.matrix_root, outputs_cfg.get("matrix_root"), DEFAULT_MATRIX_ROOT, config_dir=config_dir)
    matrix_name = str(_pick(args.matrix_name, outputs_cfg.get("matrix_name"), "recovery_matrix")).strip()
    resolved = {
        "base_manifest_path": str(Path(base_manifest).resolve()),
        "matrix_root": str((Path(matrix_root_base).resolve() / f"{matrix_name}_{_timestamp_suffix()}").resolve()),
        "existing_matrix_root": (
            _resolve_path_choice(args.matrix_root, None, matrix_root_base, config_dir=config_dir)
            if args.matrix_root is not None
            else str(Path(matrix_root_base).resolve())
        ),
        "tp_grid": _parse_float_grid(_pick(args.tp_grid, matrix_cfg.get("tp_grid"), None), DEFAULT_TP_GRID),
        "sl_grid": _parse_float_grid(_pick(args.sl_grid, matrix_cfg.get("sl_grid"), None), DEFAULT_SL_GRID),
        "horizon_grid": _parse_int_grid(_pick(args.horizon_grid, matrix_cfg.get("horizon_grid"), None), DEFAULT_HORIZON_GRID),
        "barrier_modes": _parse_name_list(_pick(args.barrier_modes, matrix_cfg.get("barrier_modes"), None), DEFAULT_BARRIER_MODES),
        "recipes": _parse_recipe_list(matrix_cfg.get("recipes")),
        "models": _parse_name_list(_pick(args.models, matrix_cfg.get("models"), None), DEFAULT_MODELS),
        "feature_sets": _parse_name_list(_pick(args.feature_sets, matrix_cfg.get("feature_sets"), None), DEFAULT_FEATURE_SETS),
        "recipe_fanout": bool(args.recipe_fanout or bool(matrix_cfg.get("recipe_fanout", False))),
        "launch_background": bool(args.launch_background or bool(launch_cfg.get("background", False))),
        "launch_pending": bool(args.launch_pending),
        "watch_pending": bool(args.watch_pending),
        "retry_failed": bool(args.retry_failed),
        "max_parallel": _pick(args.max_parallel, launch_cfg.get("max_parallel"), None),
        "poll_seconds": int(_pick(args.poll_seconds, launch_cfg.get("poll_seconds"), 120)),
        "job_root": _resolve_path_choice(args.job_root, launch_cfg.get("job_root"), DEFAULT_JOB_ROOT, config_dir=config_dir),
        "report_only": bool(args.report_only),
    }
    return resolved


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    resolved = _resolve_args(args)
    if bool(resolved["report_only"]):
        report = refresh_recovery_matrix_report(Path(resolved["existing_matrix_root"]).resolve())
        print(json.dumps(report, indent=2, default=str))
        return 0
    if bool(resolved["launch_pending"]):
        if args.matrix_root is None:
            raise ValueError("--launch-pending requires --matrix-root to point to an existing matrix directory")
        payload = launch_pending_recovery_matrix_jobs(
            Path(resolved["existing_matrix_root"]).resolve(),
            max_parallel=int(resolved["max_parallel"] or 1),
            job_root=Path(resolved["job_root"]).resolve(),
            retry_failed=bool(resolved["retry_failed"]),
        )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if bool(resolved["watch_pending"]):
        if args.matrix_root is None:
            raise ValueError("--watch-pending requires --matrix-root to point to an existing matrix directory")
        payload = watch_pending_recovery_matrix_jobs(
            Path(resolved["existing_matrix_root"]).resolve(),
            max_parallel=int(resolved["max_parallel"] or 1),
            job_root=Path(resolved["job_root"]).resolve(),
            retry_failed=bool(resolved["retry_failed"]),
            poll_seconds=int(resolved["poll_seconds"]),
        )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    index = generate_recovery_matrix(
        base_manifest_path=Path(resolved["base_manifest_path"]).resolve(),
        matrix_root=Path(resolved["matrix_root"]).resolve(),
        horizon_grid=list(resolved["horizon_grid"]),
        tp_grid=list(resolved["tp_grid"]),
        sl_grid=list(resolved["sl_grid"]),
        barrier_modes=list(resolved["barrier_modes"]),
        recipes_override=list(resolved["recipes"]),
        models=list(resolved["models"]),
        feature_sets=list(resolved["feature_sets"]),
        recipe_fanout=bool(resolved["recipe_fanout"]),
        launch_background=bool(resolved["launch_background"]),
        job_root=Path(resolved["job_root"]).resolve(),
        max_parallel_launches=(int(resolved["max_parallel"]) if resolved["max_parallel"] is not None else None),
    )
    print(json.dumps(index, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
