from __future__ import annotations

import json
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from ml_pipeline_2.contracts.manifests import RECOVERY_KIND
from ml_pipeline_2.contracts.types import RecoveryRecipe
from ml_pipeline_2.dataset_windowing import filter_trade_dates, load_feature_frame
from ml_pipeline_2.inference_contract import load_model_package, predict_probabilities_from_frame
from ml_pipeline_2.run_recovery_threshold_sweep import DEFAULT_THRESHOLD_GRID
from ml_pipeline_2.scenario_flows.fo_expiry_aware_recovery import (
    _effective_label_cfg,
    _path_reason_return,
    _prepare_labeled_frame,
    _trade_side,
    _utility_cfg,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
ML_PIPELINE_2_ARTIFACTS_ROOT = REPO_ROOT / "ml_pipeline_2" / "artifacts"
DEFAULT_DATA_ROOT = REPO_ROOT / ".data" / "ml_pipeline"
DEFAULT_DISCOVERY_ROOTS = (
    ML_PIPELINE_2_ARTIFACTS_ROOT / "research",
    ML_PIPELINE_2_ARTIFACTS_ROOT / "research_matrices",
)


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _repo_rel_text(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except Exception:
        return str(path.resolve())


def _coerce_iso_day(value: Optional[str], *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    try:
        return pd.Timestamp(text).strftime("%Y-%m-%d")
    except Exception as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except Exception:
        return float("nan")


def _profit_factor(values: Sequence[float]) -> float:
    gains = float(sum(x for x in values if np.isfinite(x) and x > 0.0))
    losses = float(-sum(x for x in values if np.isfinite(x) and x < 0.0))
    if losses <= 0.0:
        return 999.0 if gains > 0.0 else 0.0
    return float(gains / losses)


def _normalize_source_roots(roots: Optional[Sequence[Path]] = None) -> tuple[Path, ...]:
    base = roots or DEFAULT_DISCOVERY_ROOTS
    return tuple(Path(root).resolve() for root in base)


def _normalize_scenario_key(key: str) -> str:
    return str(key or "").strip().replace("\\", "/")


def _resolve_feature_path(raw: object, *, expected_name: str) -> Path:
    text = str(raw or "").strip()
    candidates: List[Path] = []
    if text:
        candidate = Path(text)
        candidates.append(candidate if candidate.is_absolute() else (REPO_ROOT / candidate))
        lower_parts = [part.lower() for part in candidate.parts]
        if "ml_pipeline" in lower_parts:
            idx = lower_parts.index("ml_pipeline")
            suffix = candidate.parts[idx + 1 :]
            if suffix:
                candidates.append(DEFAULT_DATA_ROOT.joinpath(*suffix))
        if "frozen" in lower_parts:
            idx = lower_parts.index("frozen")
            suffix = candidate.parts[idx:]
            candidates.append(DEFAULT_DATA_ROOT.joinpath(*suffix))
    candidates.append(DEFAULT_DATA_ROOT / "frozen" / expected_name)
    for item in candidates:
        try:
            resolved = item.resolve()
        except Exception:
            resolved = item
        if resolved.exists():
            return resolved
    return candidates[-1].resolve()


def _resolve_input_paths(resolved: Dict[str, Any]) -> Dict[str, Path]:
    inputs = dict(resolved.get("inputs") or {})
    return {
        "model_window_features": _resolve_feature_path(inputs.get("model_window_features_path"), expected_name="model_window_features.parquet"),
        "holdout_features": _resolve_feature_path(inputs.get("holdout_features_path"), expected_name="holdout_features.parquet"),
    }


@lru_cache(maxsize=128)
def _feature_coverage(path_text: str) -> Dict[str, Any]:
    path = Path(path_text)
    if not path.exists():
        return {"path": str(path), "exists": False, "start": None, "end": None, "days": 0}
    frame = pd.read_parquet(path, columns=["timestamp", "trade_date"])
    trade_dates = pd.to_datetime(frame.get("trade_date"), errors="coerce")
    if trade_dates.isna().all():
        trade_dates = pd.to_datetime(frame.get("timestamp"), errors="coerce")
    day_text = trade_dates.dt.strftime("%Y-%m-%d")
    valid_days = day_text.dropna()
    return {
        "path": str(path),
        "exists": True,
        "start": valid_days.min() if len(valid_days) else None,
        "end": valid_days.max() if len(valid_days) else None,
        "days": int(valid_days.nunique()) if len(valid_days) else 0,
    }


def _date_in_coverage(coverage: Dict[str, Any], *, date_from: str, date_to: str) -> bool:
    start = str(coverage.get("start") or "")
    end = str(coverage.get("end") or "")
    if not start or not end:
        return False
    return not (date_to < start or date_from > end)


def _extract_recipe_rows(summary: Dict[str, Any], resolved: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = list(summary.get("primary_recipes") or [])
    if rows:
        return [dict(row or {}) for row in rows]
    fallback = []
    for recipe in list((resolved.get("scenario") or {}).get("recipes") or []):
        payload = dict(recipe or {})
        if payload:
            fallback.append({"recipe": payload})
    return fallback


def _extract_recipe_id(row: Dict[str, Any]) -> str:
    recipe = dict(row.get("recipe") or {})
    return str(recipe.get("recipe_id") or "").strip()


def _threshold_sweep_summary(run_dir: Path, recipe_id: str) -> Optional[Dict[str, Any]]:
    summary_path = run_dir / "primary_recipes" / recipe_id / "threshold_sweep" / "summary.json"
    if not summary_path.exists():
        return None
    try:
        return _read_json(summary_path)
    except Exception:
        return None


def _pick_default_threshold(*, run_dir: Path, resolved: Dict[str, Any], recipe_id: str, explicit_threshold: Optional[float]) -> tuple[float, str, Optional[float]]:
    if explicit_threshold is not None:
        return float(explicit_threshold), "explicit", None
    sweep = _threshold_sweep_summary(run_dir, recipe_id)
    if isinstance(sweep, dict):
        recommended = sweep.get("recommended_threshold")
        if recommended is not None:
            return float(recommended), "recommended_cached", float(recommended)
    current = float(((resolved.get("scenario") or {}).get("primary_threshold")) or ((resolved.get("training") or {}).get("utility") or {}).get("ce_threshold") or DEFAULT_THRESHOLD_GRID[0])
    return current, "current", None


def _range_after_full_model(windows: Dict[str, Any], *, date_from: str, date_to: str) -> None:
    full_model = dict(windows.get("full_model") or {})
    full_end = str(full_model.get("end") or "").strip()
    if full_end and date_from <= full_end:
        next_allowed = (pd.Timestamp(full_end) + timedelta(days=1)).strftime("%Y-%m-%d")
        raise ValueError(
            f"Requested range {date_from} to {date_to} overlaps the model window ending {full_end}. "
            f"Choose a range starting on or after {next_allowed}."
        )


def _allowed_eval_window(resolved: Dict[str, Any], *, input_paths: Dict[str, Path]) -> Dict[str, Optional[str]]:
    windows = dict(resolved.get("windows") or {})
    full_end = str((windows.get("full_model") or {}).get("end") or "").strip()
    holdout = dict(windows.get("final_holdout") or {})
    holdout_start = str(holdout.get("start") or "").strip() or None
    holdout_end = str(holdout.get("end") or "").strip() or None
    available_starts: List[str] = []
    available_ends: List[str] = []
    for path in input_paths.values():
        coverage = _feature_coverage(str(path.resolve()))
        start = str(coverage.get("start") or "").strip()
        end = str(coverage.get("end") or "").strip()
        if start:
            available_starts.append(start)
        if end:
            available_ends.append(end)
    data_start = min(available_starts) if available_starts else None
    data_end = max(available_ends) if available_ends else None
    next_after_full = (pd.Timestamp(full_end) + timedelta(days=1)).strftime("%Y-%m-%d") if full_end else data_start
    allowed_start = max([value for value in (next_after_full, data_start) if value], default=None)
    allowed_end = data_end
    return {
        "data_start": data_start,
        "data_end": data_end,
        "allowed_start": allowed_start,
        "allowed_end": allowed_end,
        "default_start": holdout_start or allowed_start,
        "default_end": holdout_end or allowed_end,
    }


def _load_run_payload(run_dir: Path) -> Dict[str, Any]:
    summary = _read_json(run_dir / "summary.json")
    resolved = _read_json(run_dir / "resolved_config.json")
    return {"summary": summary, "resolved": resolved}


def _iter_recovery_run_dirs(*, roots: Optional[Sequence[Path]] = None) -> Iterable[Path]:
    seen: set[Path] = set()
    for root in _normalize_source_roots(roots):
        if not root.exists():
            continue
        for resolved_path in root.rglob("resolved_config.json"):
            run_dir = resolved_path.parent
            if run_dir in seen:
                continue
            summary_path = run_dir / "summary.json"
            if not summary_path.exists():
                continue
            try:
                resolved = _read_json(resolved_path)
            except Exception:
                continue
            if str(resolved.get("experiment_kind") or "").strip() != RECOVERY_KIND:
                continue
            seen.add(run_dir)
            yield run_dir


def _scenario_payload(run_dir: Path) -> Dict[str, Any]:
    payload = _load_run_payload(run_dir)
    summary = payload["summary"]
    resolved = payload["resolved"]
    input_paths = _resolve_input_paths(resolved)
    windows = dict(resolved.get("windows") or {})
    recipe_rows = _extract_recipe_rows(summary, resolved)
    default_recipe_id = str(summary.get("selected_primary_recipe_id") or "").strip()
    if not default_recipe_id and recipe_rows:
        default_recipe_id = _extract_recipe_id(recipe_rows[0])
    allowed = _allowed_eval_window(resolved, input_paths=input_paths)
    title = str((resolved.get("outputs") or {}).get("run_name") or run_dir.name).strip() or run_dir.name
    catalog = dict(resolved.get("catalog") or {})
    recipes: List[Dict[str, Any]] = []
    current_threshold = float(((resolved.get("scenario") or {}).get("primary_threshold")) or ((resolved.get("training") or {}).get("utility") or {}).get("ce_threshold") or DEFAULT_THRESHOLD_GRID[0])
    for row in recipe_rows:
        recipe = dict(row.get("recipe") or {})
        recipe_id = _extract_recipe_id(row)
        holdout_summary = dict(row.get("holdout_summary") or {})
        sweep = _threshold_sweep_summary(run_dir, recipe_id) if recipe_id else None
        stage_eval = dict(holdout_summary.get("stage_eval") or {})
        stage_b = dict(stage_eval.get("stage_b_futures_utility") or {})
        recipes.append(
            {
                "recipe_id": recipe_id,
                "horizon_minutes": recipe.get("horizon_minutes"),
                "take_profit_pct": recipe.get("take_profit_pct"),
                "stop_loss_pct": recipe.get("stop_loss_pct"),
                "barrier_mode": recipe.get("barrier_mode"),
                "train_rows": row.get("train_rows"),
                "holdout_rows": row.get("holdout_rows"),
                "default_threshold": current_threshold,
                "recommended_threshold": (sweep.get("recommended_threshold") if isinstance(sweep, dict) else None),
                "holdout_metrics": {
                    "trades": holdout_summary.get("trades"),
                    "win_rate": holdout_summary.get("win_rate"),
                    "profit_factor": holdout_summary.get("profit_factor"),
                    "net_return_sum": holdout_summary.get("net_return_sum"),
                    "stage_a_passed": holdout_summary.get("stage_a_passed"),
                    "stage_b_status": stage_b.get("status"),
                },
            }
        )
    return {
        "scenario_key": _repo_rel_text(run_dir),
        "title": title,
        "run_dir": _repo_rel_text(run_dir),
        "run_name": title,
        "status": str(summary.get("status") or "unknown"),
        "created_at_utc": summary.get("created_at_utc"),
        "experiment_kind": str(resolved.get("experiment_kind") or ""),
        "feature_profile": catalog.get("feature_profile"),
        "feature_sets": list(catalog.get("feature_sets") or []),
        "models": list(catalog.get("models") or []),
        "primary_model": (resolved.get("scenario") or {}).get("primary_model"),
        "default_recipe_id": default_recipe_id,
        "default_threshold": current_threshold,
        "windows": {
            "full_model": windows.get("full_model"),
            "final_holdout": windows.get("final_holdout"),
        },
        "eval_window": allowed,
        "recipes": recipes,
    }


def list_recovery_scenarios(*, roots: Optional[Sequence[Path]] = None) -> Dict[str, Any]:
    scenarios = [_scenario_payload(run_dir) for run_dir in _iter_recovery_run_dirs(roots=roots)]
    scenarios.sort(
        key=lambda item: (
            str(item.get("created_at_utc") or ""),
            str(item.get("scenario_key") or ""),
        ),
        reverse=True,
    )
    return {
        "status": "ok",
        "count": len(scenarios),
        "scenarios": scenarios,
    }


def _resolve_scenario_dir(scenario_key: str, *, roots: Optional[Sequence[Path]] = None) -> Path:
    normalized = _normalize_scenario_key(scenario_key)
    for run_dir in _iter_recovery_run_dirs(roots=roots):
        if _normalize_scenario_key(_repo_rel_text(run_dir)) == normalized:
            return run_dir
    raise FileNotFoundError(f"recovery scenario not found: {scenario_key}")


def _resolve_recipe(run_dir: Path, resolved: Dict[str, Any], summary: Dict[str, Any], recipe_id: Optional[str]) -> RecoveryRecipe:
    selected = str(recipe_id or summary.get("selected_primary_recipe_id") or "").strip()
    rows = _extract_recipe_rows(summary, resolved)
    if selected:
        for row in rows:
            if _extract_recipe_id(row) == selected:
                return RecoveryRecipe(**dict(row.get("recipe") or {}))
    if rows:
        return RecoveryRecipe(**dict(rows[0].get("recipe") or {}))
    raise ValueError(f"no recovery recipes found under run: {run_dir}")


def _load_eval_features(*, input_paths: Dict[str, Path], date_from: str, date_to: str) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for path in (input_paths["model_window_features"], input_paths["holdout_features"]):
        coverage = _feature_coverage(str(path.resolve()))
        if not _date_in_coverage(coverage, date_from=date_from, date_to=date_to):
            continue
        filtered = filter_trade_dates(load_feature_frame(path), date_from, date_to)
        if len(filtered):
            frames.append(filtered)
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0].copy().reset_index(drop=True)
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    return out


@lru_cache(maxsize=64)
def _build_effective_label_cfg(run_dir_text: str, recipe_id: str) -> Any:
    run_dir = Path(run_dir_text)
    payload = _load_run_payload(run_dir)
    resolved = payload["resolved"]
    summary = payload["summary"]
    recipe = _resolve_recipe(run_dir, resolved, summary, recipe_id=recipe_id)
    input_paths = _resolve_input_paths(resolved)
    windows = dict(resolved.get("windows") or {})
    scenario = dict(resolved.get("scenario") or {})
    model_window = filter_trade_dates(
        load_feature_frame(input_paths["model_window_features"]),
        str((windows.get("full_model") or {}).get("start") or ""),
        str((windows.get("full_model") or {}).get("end") or ""),
    )
    return _effective_label_cfg(
        recipe,
        train_features=model_window,
        event_sampling_mode=str(scenario.get("event_sampling_mode", "none")),
        event_signal_col=scenario.get("event_signal_col"),
    )


def _lookup_price(frame: pd.DataFrame, *, ts: pd.Timestamp, field: str, fallback: Optional[float] = None) -> Optional[float]:
    if pd.isna(ts):
        return fallback
    match = frame.loc[frame["timestamp"] == ts]
    if len(match):
        value = _safe_float(match.iloc[0].get(field))
        if np.isfinite(value):
            return float(value)
    return fallback


def _outcome_label(value: float) -> str:
    if not np.isfinite(value):
        return "unknown"
    if value > 0.0:
        return "win"
    if value < 0.0:
        return "loss"
    return "flat"


def _build_trade_rows(
    *,
    labeled: pd.DataFrame,
    probs: pd.DataFrame,
    threshold: float,
    cost_per_trade: float,
) -> pd.DataFrame:
    frame = labeled.reset_index(drop=True).copy()
    score_frame = probs.reset_index(drop=True).copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["ce_prob"] = pd.to_numeric(score_frame.get("ce_prob"), errors="coerce")
    frame["pe_prob"] = pd.to_numeric(score_frame.get("pe_prob"), errors="coerce")

    rows: List[Dict[str, Any]] = []
    for idx, row in enumerate(frame.itertuples(index=False), start=1):
        series = pd.Series(row._asdict())
        ce_prob = _safe_float(series.get("ce_prob"))
        pe_prob = _safe_float(series.get("pe_prob"))
        chosen_side = _trade_side(ce_prob, pe_prob, float(threshold))
        if chosen_side is None:
            continue
        prefix = "ce" if str(chosen_side).upper() == "CE" else "pe"
        decision_ts = pd.to_datetime(series.get("timestamp"), errors="coerce")
        entry_ts = decision_ts + pd.Timedelta(minutes=1) if pd.notna(decision_ts) else pd.NaT
        planned_exit_ts = decision_ts + pd.Timedelta(minutes=int(_safe_float(series.get("label_horizon_minutes")) or 0.0)) if pd.notna(decision_ts) else pd.NaT
        event_end_ts = pd.to_datetime(series.get(f"{prefix}_event_end_ts"), errors="coerce")
        exit_ts = event_end_ts if pd.notna(event_end_ts) else planned_exit_ts
        exit_reason = str(series.get(f"{prefix}_path_exit_reason") or "").strip().lower()
        gross_return = _path_reason_return(series, chosen_side)
        gross_value = float(gross_return) if gross_return is not None and np.isfinite(gross_return) else float("nan")
        net_return = gross_value - float(cost_per_trade) if np.isfinite(gross_value) else float("nan")
        entry_fut_price = _lookup_price(frame, ts=entry_ts, field="px_fut_open", fallback=_safe_float(series.get("px_fut_close")))
        exit_fut_price = _lookup_price(frame, ts=exit_ts, field="px_fut_close", fallback=_safe_float(series.get("px_fut_close")))
        rows.append(
            {
                "trade_id": f"T{idx:04d}",
                "trade_date": str(series.get("trade_date") or ""),
                "decision_ts": decision_ts,
                "entry_ts": entry_ts,
                "planned_exit_ts": planned_exit_ts,
                "event_end_ts": event_end_ts,
                "exit_ts": exit_ts,
                "threshold": float(threshold),
                "chosen_side": str(chosen_side).upper(),
                "chosen_direction": "UP" if str(chosen_side).upper() == "CE" else "DOWN",
                "ce_prob": ce_prob,
                "pe_prob": pe_prob,
                "chosen_prob": float(ce_prob if str(chosen_side).upper() == "CE" else pe_prob),
                "prob_gap": float(abs(ce_prob - pe_prob)) if np.isfinite(ce_prob) and np.isfinite(pe_prob) else float("nan"),
                "exit_reason": exit_reason,
                "entry_fut_price": entry_fut_price,
                "exit_fut_price": exit_fut_price,
                "gross_return": gross_value,
                "net_return_after_cost": net_return,
                "gross_outcome": _outcome_label(gross_value),
                "net_outcome": _outcome_label(net_return),
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=[
                "trade_id",
                "trade_date",
                "decision_ts",
                "entry_ts",
                "planned_exit_ts",
                "event_end_ts",
                "exit_ts",
                "threshold",
                "chosen_side",
                "chosen_direction",
                "ce_prob",
                "pe_prob",
                "chosen_prob",
                "prob_gap",
                "exit_reason",
                "entry_fut_price",
                "exit_fut_price",
                "gross_return",
                "net_return_after_cost",
                "gross_outcome",
                "net_outcome",
            ]
        )
    return pd.DataFrame(rows).sort_values(["entry_ts", "trade_id"]).reset_index(drop=True)


def _daily_rows(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    if len(trades) == 0:
        return []
    rows: List[Dict[str, Any]] = []
    for day, part in trades.groupby("trade_date", dropna=False):
        net = pd.to_numeric(part["net_return_after_cost"], errors="coerce").to_numpy(dtype=float)
        rows.append(
            {
                "trade_date": str(day or ""),
                "trades": int(len(part)),
                "win_rate": float(np.mean(net > 0.0)) if len(part) else 0.0,
                "net_return_sum": float(np.nansum(net)) if len(part) else 0.0,
                "profit_factor": float(_profit_factor(net)),
            }
        )
    rows.sort(key=lambda item: str(item.get("trade_date") or ""))
    return rows


def _serialize_ts(value: object) -> Optional[str]:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.isoformat()


def _chart_payload(
    *,
    labeled: pd.DataFrame,
    probs: pd.DataFrame,
    trades: pd.DataFrame,
    threshold: float,
) -> Dict[str, Any]:
    bars = []
    for row in labeled.itertuples(index=False):
        bars.append(
            {
                "ts": _serialize_ts(getattr(row, "timestamp", None)),
                "trade_date": str(getattr(row, "trade_date", "") or ""),
                "open": _safe_float(getattr(row, "px_fut_open", np.nan)),
                "high": _safe_float(getattr(row, "px_fut_high", np.nan)),
                "low": _safe_float(getattr(row, "px_fut_low", np.nan)),
                "close": _safe_float(getattr(row, "px_fut_close", np.nan)),
            }
        )
    probability_rows = []
    merged = labeled.reset_index(drop=True).copy()
    merged["ce_prob"] = pd.to_numeric(probs.get("ce_prob"), errors="coerce")
    merged["pe_prob"] = pd.to_numeric(probs.get("pe_prob"), errors="coerce")
    for row in merged.itertuples(index=False):
        probability_rows.append(
            {
                "ts": _serialize_ts(getattr(row, "timestamp", None)),
                "ce_prob": _safe_float(getattr(row, "ce_prob", np.nan)),
                "pe_prob": _safe_float(getattr(row, "pe_prob", np.nan)),
            }
        )
    entry_markers = []
    exit_markers = []
    for trade in trades.itertuples(index=False):
        entry_markers.append(
            {
                "trade_id": str(getattr(trade, "trade_id", "")),
                "ts": _serialize_ts(getattr(trade, "entry_ts", None)),
                "price": _safe_float(getattr(trade, "entry_fut_price", np.nan)),
                "side": str(getattr(trade, "chosen_side", "")),
                "prob": _safe_float(getattr(trade, "chosen_prob", np.nan)),
                "net_return_after_cost": _safe_float(getattr(trade, "net_return_after_cost", np.nan)),
            }
        )
        exit_markers.append(
            {
                "trade_id": str(getattr(trade, "trade_id", "")),
                "ts": _serialize_ts(getattr(trade, "exit_ts", None)),
                "price": _safe_float(getattr(trade, "exit_fut_price", np.nan)),
                "side": str(getattr(trade, "chosen_side", "")),
                "exit_reason": str(getattr(trade, "exit_reason", "")),
                "net_return_after_cost": _safe_float(getattr(trade, "net_return_after_cost", np.nan)),
            }
        )
    return {
        "bars": bars,
        "probabilities": probability_rows,
        "entry_markers": entry_markers,
        "exit_markers": exit_markers,
        "threshold": float(threshold),
    }


def _summary_payload(*, labeled: pd.DataFrame, trades: pd.DataFrame, threshold: float) -> Dict[str, Any]:
    net_returns = pd.to_numeric(trades.get("net_return_after_cost"), errors="coerce").to_numpy(dtype=float) if len(trades) else np.asarray([], dtype=float)
    return {
        "rows_total": int(len(labeled)),
        "days_total": int(labeled["trade_date"].nunique()) if len(labeled) and "trade_date" in labeled.columns else 0,
        "trades": int(len(trades)),
        "blocked_rows": int(len(labeled) - len(trades)),
        "trade_rate": float(len(trades) / len(labeled)) if len(labeled) else 0.0,
        "long_trades": int((trades["chosen_side"] == "CE").sum()) if len(trades) else 0,
        "short_trades": int((trades["chosen_side"] == "PE").sum()) if len(trades) else 0,
        "win_rate": float(np.mean(net_returns > 0.0)) if len(trades) else 0.0,
        "net_return_sum": float(np.nansum(net_returns)) if len(trades) else 0.0,
        "mean_net_return_per_trade": float(np.nanmean(net_returns)) if len(trades) else 0.0,
        "profit_factor": float(_profit_factor(net_returns)),
        "threshold": float(threshold),
    }


def evaluate_recovery_scenario(
    *,
    scenario_key: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    recipe_id: Optional[str] = None,
    threshold: Optional[float] = None,
    roots: Optional[Sequence[Path]] = None,
) -> Dict[str, Any]:
    run_dir = _resolve_scenario_dir(scenario_key, roots=roots)
    payload = _load_run_payload(run_dir)
    summary = payload["summary"]
    resolved = payload["resolved"]
    scenario_meta = _scenario_payload(run_dir)
    input_paths = _resolve_input_paths(resolved)
    eval_window = dict(scenario_meta.get("eval_window") or {})
    requested_from = _coerce_iso_day(date_from or eval_window.get("default_start"), field="date_from")
    requested_to = _coerce_iso_day(date_to or eval_window.get("default_end"), field="date_to")
    if requested_from > requested_to:
        raise ValueError("date_from must be on or before date_to")
    _range_after_full_model(dict(resolved.get("windows") or {}), date_from=requested_from, date_to=requested_to)
    allowed_start = str(eval_window.get("allowed_start") or "").strip()
    allowed_end = str(eval_window.get("allowed_end") or "").strip()
    if allowed_start and requested_from < allowed_start:
        raise ValueError(
            f"Requested range starts on {requested_from}, but this scenario only supports out-of-sample dates on or after {allowed_start}."
        )
    if allowed_end and requested_to > allowed_end:
        raise ValueError(
            f"Requested range ends on {requested_to}, but available cached data ends on {allowed_end}."
        )

    recipe = _resolve_recipe(run_dir, resolved, summary, recipe_id=recipe_id)
    chosen_threshold, threshold_source, recommended_threshold = _pick_default_threshold(
        run_dir=run_dir,
        resolved=resolved,
        recipe_id=recipe.recipe_id,
        explicit_threshold=threshold,
    )
    if chosen_threshold < 0.0 or chosen_threshold > 1.0:
        raise ValueError("threshold must be in [0,1]")
    features = _load_eval_features(input_paths=input_paths, date_from=requested_from, date_to=requested_to)
    if len(features) == 0:
        return {
            "status": "no_data",
            "message": f"No feature rows found for {requested_from} to {requested_to}.",
            "scenario": scenario_meta,
            "request": {
                "date_from": requested_from,
                "date_to": requested_to,
                "recipe_id": recipe.recipe_id,
                "threshold": float(chosen_threshold),
                "threshold_source": threshold_source,
            },
            "summary": {
                "rows_total": 0,
                "days_total": 0,
                "trades": 0,
                "blocked_rows": 0,
                "trade_rate": 0.0,
                "long_trades": 0,
                "short_trades": 0,
                "win_rate": 0.0,
                "net_return_sum": 0.0,
                "mean_net_return_per_trade": 0.0,
                "profit_factor": 0.0,
                "threshold": float(chosen_threshold),
            },
            "chart": {"bars": [], "probabilities": [], "entry_markers": [], "exit_markers": [], "threshold": float(chosen_threshold)},
            "daily": [],
            "trades": [],
        }

    label_cfg = _build_effective_label_cfg(str(run_dir.resolve()), recipe.recipe_id)
    scenario_cfg = dict(resolved.get("scenario") or {})
    labeled, _, _ = _prepare_labeled_frame(
        features,
        recipe=recipe,
        label_cfg=label_cfg,
        event_sampling_mode=str(scenario_cfg.get("event_sampling_mode", "none")),
        context=f"dashboard.research_eval:{recipe.recipe_id}:{requested_from}:{requested_to}",
    )
    recipe_root = run_dir / "primary_recipes" / recipe.recipe_id
    model_path = recipe_root / "model.joblib"
    if not model_path.exists():
        raise FileNotFoundError(f"model.joblib not found for recipe {recipe.recipe_id}: {model_path}")
    model_package = load_model_package(model_path)
    probs, _ = predict_probabilities_from_frame(
        labeled,
        model_package,
        missing_policy_override="error",
        context=f"dashboard.research_eval:{recipe.recipe_id}",
    )
    if "ce_prob" not in probs.columns or "pe_prob" not in probs.columns:
        raise ValueError(f"scenario is not a dual-side recovery model: {recipe.recipe_id}")

    utility_cfg = _utility_cfg(dict((resolved.get("training") or {}).get("utility") or {}))
    trades = _build_trade_rows(
        labeled=labeled,
        probs=probs,
        threshold=float(chosen_threshold),
        cost_per_trade=float(utility_cfg.cost_per_trade),
    )

    return {
        "status": "ok",
        "scenario": scenario_meta,
        "request": {
            "date_from": requested_from,
            "date_to": requested_to,
            "recipe_id": recipe.recipe_id,
            "threshold": float(chosen_threshold),
            "threshold_source": threshold_source,
            "recommended_threshold": recommended_threshold,
        },
        "summary": _summary_payload(labeled=labeled, trades=trades, threshold=float(chosen_threshold)),
        "chart": _chart_payload(labeled=labeled, probs=probs, trades=trades, threshold=float(chosen_threshold)),
        "daily": _daily_rows(trades),
        "trades": [
            {
                "trade_id": str(row["trade_id"]),
                "trade_date": str(row["trade_date"]),
                "decision_ts": _serialize_ts(row["decision_ts"]),
                "entry_ts": _serialize_ts(row["entry_ts"]),
                "exit_ts": _serialize_ts(row["exit_ts"]),
                "chosen_side": str(row["chosen_side"]),
                "chosen_direction": str(row["chosen_direction"]),
                "ce_prob": (None if not np.isfinite(_safe_float(row["ce_prob"])) else float(row["ce_prob"])),
                "pe_prob": (None if not np.isfinite(_safe_float(row["pe_prob"])) else float(row["pe_prob"])),
                "chosen_prob": (None if not np.isfinite(_safe_float(row["chosen_prob"])) else float(row["chosen_prob"])),
                "prob_gap": (None if not np.isfinite(_safe_float(row["prob_gap"])) else float(row["prob_gap"])),
                "exit_reason": str(row["exit_reason"]),
                "entry_fut_price": (None if not np.isfinite(_safe_float(row["entry_fut_price"])) else float(row["entry_fut_price"])),
                "exit_fut_price": (None if not np.isfinite(_safe_float(row["exit_fut_price"])) else float(row["exit_fut_price"])),
                "gross_return": (None if not np.isfinite(_safe_float(row["gross_return"])) else float(row["gross_return"])),
                "net_return_after_cost": (None if not np.isfinite(_safe_float(row["net_return_after_cost"])) else float(row["net_return_after_cost"])),
                "net_outcome": str(row["net_outcome"]),
            }
            for _, row in trades.iterrows()
        ],
    }
