from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .contracts.manifests import RECOVERY_KIND
from .scenario_flows.fo_expiry_aware_recovery import _path_reason_return, _trade_side, _utility_cfg
from .run_recovery_threshold_sweep import sweep_recovery_thresholds


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except Exception:
        return float("nan")


def _outcome_label(value: float) -> str:
    if not np.isfinite(value):
        return "unknown"
    if float(value) > 0.0:
        return "win"
    if float(value) < 0.0:
        return "loss"
    return "flat"


def _profit_factor(values: Sequence[float]) -> float:
    gains = float(sum(x for x in values if np.isfinite(x) and x > 0.0))
    losses = float(-sum(x for x in values if np.isfinite(x) and x < 0.0))
    if losses <= 0.0:
        return 999.0 if gains > 0.0 else 0.0
    return float(gains / losses)


def _resolve_recipe_id(*, run_dir: Path, summary: Optional[Dict[str, Any]], recipe_id: Optional[str]) -> str:
    chosen = str(recipe_id or "").strip()
    if chosen:
        return chosen
    selected = str((summary or {}).get("selected_primary_recipe_id") or "").strip()
    if selected:
        return selected
    raise ValueError(f"recipe_id is required when run summary does not expose selected_primary_recipe_id: {run_dir}")


def _load_or_build_threshold_artifacts(*, run_dir: Path, recipe_id: str) -> Dict[str, Any]:
    recipe_root = run_dir / "primary_recipes" / recipe_id
    sweep_root = recipe_root / "threshold_sweep"
    summary_path = sweep_root / "summary.json"
    labeled_path = sweep_root / "holdout_labeled.parquet"
    probs_path = sweep_root / "holdout_probabilities.parquet"
    if summary_path.exists() and labeled_path.exists() and probs_path.exists():
        return _read_json(summary_path)
    return sweep_recovery_thresholds(run_dir=run_dir, recipe_id=recipe_id)


def _resolve_threshold(
    *,
    threshold: Optional[float],
    threshold_source: str,
    sweep_summary: Dict[str, Any],
    resolved: Dict[str, Any],
) -> tuple[float, str]:
    if threshold is not None:
        return float(threshold), "explicit"
    if str(threshold_source).strip().lower() == "current":
        return float(sweep_summary.get("primary_threshold") or ((resolved.get("scenario") or {}).get("primary_threshold") or 0.25)), "current"
    recommended = sweep_summary.get("recommended_threshold")
    if recommended is not None:
        return float(recommended), "recommended"
    return float(sweep_summary.get("primary_threshold") or ((resolved.get("scenario") or {}).get("primary_threshold") or 0.25)), "current_fallback"


def _realized_exit_price(*, row: pd.Series, prefix: str, exit_reason: str) -> float:
    if exit_reason in {"tp", "tp_sl_same_bar"}:
        return _safe_float(row.get(f"{prefix}_tp_price"))
    if exit_reason == "sl":
        return _safe_float(row.get(f"{prefix}_sl_price"))
    return _safe_float(row.get(f"{prefix}_exit_price"))


def _reason_implied_outcome(exit_reason: str) -> str:
    reason = str(exit_reason).strip().lower()
    if reason in {"tp", "tp_sl_same_bar"}:
        return "win"
    if reason == "sl":
        return "loss"
    if reason == "time_stop":
        return "depends"
    return "unknown"


def _chosen_trade_rows(
    *,
    holdout_labeled: pd.DataFrame,
    probs: pd.DataFrame,
    threshold: float,
    cost_per_trade: float,
    horizon_minutes: int,
) -> pd.DataFrame:
    frame = holdout_labeled.reset_index(drop=True).copy()
    score_frame = probs.reset_index(drop=True).copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    if "trade_date" in frame.columns:
        frame["trade_date"] = frame["trade_date"].astype(str)
    else:
        frame["trade_date"] = frame["timestamp"].dt.strftime("%Y-%m-%d")
    frame["ce_prob"] = pd.to_numeric(score_frame.get("ce_prob"), errors="coerce")
    frame["pe_prob"] = pd.to_numeric(score_frame.get("pe_prob"), errors="coerce")

    rows: List[Dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        series = pd.Series(row._asdict())
        ce_prob = _safe_float(series.get("ce_prob"))
        pe_prob = _safe_float(series.get("pe_prob"))
        chosen_side = _trade_side(ce_prob, pe_prob, float(threshold))
        if chosen_side is None:
            continue
        prefix = "ce" if str(chosen_side).upper() == "CE" else "pe"
        opposite_prefix = "pe" if prefix == "ce" else "ce"
        decision_ts = pd.to_datetime(series.get("timestamp"), errors="coerce")
        entry_ts = decision_ts + pd.Timedelta(minutes=1) if pd.notna(decision_ts) else pd.NaT
        planned_exit_ts = decision_ts + pd.Timedelta(minutes=int(horizon_minutes)) if pd.notna(decision_ts) else pd.NaT
        event_end_ts = pd.to_datetime(series.get(f"{prefix}_event_end_ts"), errors="coerce")
        exit_reason = str(series.get(f"{prefix}_path_exit_reason") or "").strip().lower()
        gross_return = _path_reason_return(series, chosen_side)
        net_return = float(gross_return - float(cost_per_trade)) if gross_return is not None and np.isfinite(gross_return) else float("nan")
        row_out = {
            "trade_date": str(series.get("trade_date") or ""),
            "decision_ts": decision_ts,
            "entry_ts": entry_ts,
            "planned_exit_ts": planned_exit_ts,
            "event_end_ts": event_end_ts,
            "threshold": float(threshold),
            "chosen_side": str(chosen_side).upper(),
            "chosen_direction": ("UP" if str(chosen_side).upper() == "CE" else "DOWN"),
            "ce_prob": ce_prob,
            "pe_prob": pe_prob,
            "chosen_prob": float(ce_prob if str(chosen_side).upper() == "CE" else pe_prob),
            "prob_gap": float(abs(ce_prob - pe_prob)) if np.isfinite(ce_prob) and np.isfinite(pe_prob) else float("nan"),
            "exit_reason": exit_reason,
            "reason_implied_outcome": _reason_implied_outcome(exit_reason),
            "tp_hit": int(_safe_float(series.get(f"{prefix}_tp_hit")) == 1.0),
            "sl_hit": int(_safe_float(series.get(f"{prefix}_sl_hit")) == 1.0),
            "time_stop_exit": int(_safe_float(series.get(f"{prefix}_time_stop_exit")) == 1.0),
            "first_hit_offset_min": _safe_float(series.get(f"{prefix}_first_hit_offset_min")),
            "entry_price": _safe_float(series.get(f"{prefix}_entry_price")),
            "vertical_exit_price": _safe_float(series.get(f"{prefix}_exit_price")),
            "realized_exit_price": _realized_exit_price(row=series, prefix=prefix, exit_reason=exit_reason),
            "tp_price": _safe_float(series.get(f"{prefix}_tp_price")),
            "sl_price": _safe_float(series.get(f"{prefix}_sl_price")),
            "barrier_upper_return": _safe_float(series.get(f"{prefix}_barrier_upper_return")),
            "barrier_lower_return": _safe_float(series.get(f"{prefix}_barrier_lower_return")),
            "chosen_forward_return": _safe_float(series.get(f"{prefix}_forward_return")),
            "chosen_realized_return": _safe_float(series.get(f"{prefix}_realized_return")),
            "chosen_mfe": _safe_float(series.get(f"{prefix}_mfe")),
            "chosen_mae": _safe_float(series.get(f"{prefix}_mae")),
            "opposite_forward_return": _safe_float(series.get(f"{opposite_prefix}_forward_return")),
            "opposite_exit_reason": str(series.get(f"{opposite_prefix}_path_exit_reason") or "").strip().lower(),
            "gross_return": float(gross_return) if gross_return is not None and np.isfinite(gross_return) else float("nan"),
            "net_return_after_cost": net_return,
            "gross_outcome": _outcome_label(float(gross_return) if gross_return is not None and np.isfinite(gross_return) else float("nan")),
            "net_outcome": _outcome_label(net_return),
            "moved_in_predicted_direction": bool(gross_return is not None and np.isfinite(gross_return) and float(gross_return) > 0.0),
        }
        rows.append(row_out)
    out = pd.DataFrame(rows)
    if len(out) == 0:
        return out
    return out.sort_values(["decision_ts", "chosen_side"]).reset_index(drop=True)


def _group_summary(frame: pd.DataFrame, *, by: str) -> pd.DataFrame:
    if len(frame) == 0:
        return pd.DataFrame(columns=[by, "trades", "gross_win_rate", "net_win_rate", "gross_return_sum", "net_return_sum", "gross_profit_factor", "net_profit_factor"])
    rows: List[Dict[str, Any]] = []
    for group_value, part in frame.groupby(by, dropna=False):
        gross = pd.to_numeric(part["gross_return"], errors="coerce").to_numpy(dtype=float)
        net = pd.to_numeric(part["net_return_after_cost"], errors="coerce").to_numpy(dtype=float)
        rows.append(
            {
                by: group_value,
                "trades": int(len(part)),
                "gross_win_rate": float(np.mean(gross > 0.0)) if len(part) else 0.0,
                "net_win_rate": float(np.mean(net > 0.0)) if len(part) else 0.0,
                "gross_return_sum": float(np.nansum(gross)),
                "net_return_sum": float(np.nansum(net)),
                "gross_profit_factor": float(_profit_factor(gross)),
                "net_profit_factor": float(_profit_factor(net)),
            }
        )
    return pd.DataFrame(rows)


def _resolve_output_dir(*, run_dir: Path, recipe_id: str, threshold: float, output_dir: Optional[Path]) -> Path:
    if output_dir is not None:
        return output_dir
    return run_dir / "primary_recipes" / recipe_id / "trade_report" / f"threshold_{float(threshold):.2f}"


def build_recovery_trade_report(
    *,
    run_dir: Path,
    recipe_id: Optional[str] = None,
    threshold: Optional[float] = None,
    threshold_source: str = "recommended",
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    resolved_path = run_dir / "resolved_config.json"
    if not resolved_path.exists():
        raise FileNotFoundError(f"resolved_config.json not found: {resolved_path}")
    resolved = _read_json(resolved_path)
    if str(resolved.get("experiment_kind") or "").strip() != RECOVERY_KIND:
        raise ValueError(f"run is not a recovery experiment: {run_dir}")
    run_summary_path = run_dir / "summary.json"
    run_summary = _read_json(run_summary_path) if run_summary_path.exists() else None
    chosen_recipe_id = _resolve_recipe_id(run_dir=run_dir, summary=run_summary, recipe_id=recipe_id)
    sweep_summary = _load_or_build_threshold_artifacts(run_dir=run_dir, recipe_id=chosen_recipe_id)
    chosen_threshold, resolved_threshold_source = _resolve_threshold(
        threshold=threshold,
        threshold_source=threshold_source,
        sweep_summary=sweep_summary,
        resolved=resolved,
    )
    recipe_payload = dict(sweep_summary.get("recipe") or {})
    horizon_minutes = int(recipe_payload.get("horizon_minutes") or 0)
    utility_cfg = _utility_cfg(dict((resolved.get("training") or {}).get("utility") or {}))
    holdout_labeled_path = Path(str((sweep_summary.get("paths") or {}).get("holdout_labeled") or ""))
    holdout_probabilities_path = Path(str((sweep_summary.get("paths") or {}).get("holdout_probabilities") or ""))
    if not holdout_labeled_path.exists() or not holdout_probabilities_path.exists():
        raise FileNotFoundError(f"threshold sweep artifacts missing for recipe_id={chosen_recipe_id}: {holdout_labeled_path} / {holdout_probabilities_path}")
    holdout_labeled = pd.read_parquet(holdout_labeled_path)
    probs = pd.read_parquet(holdout_probabilities_path)
    trades = _chosen_trade_rows(
        holdout_labeled=holdout_labeled,
        probs=probs,
        threshold=float(chosen_threshold),
        cost_per_trade=float(utility_cfg.cost_per_trade),
        horizon_minutes=int(horizon_minutes),
    )
    exit_reason_summary = _group_summary(trades, by="exit_reason")
    daily_summary = _group_summary(trades, by="trade_date")
    trade_root = _resolve_output_dir(run_dir=run_dir, recipe_id=chosen_recipe_id, threshold=float(chosen_threshold), output_dir=output_dir)
    trade_root.mkdir(parents=True, exist_ok=True)
    trades_csv_path = trade_root / "trades.csv"
    trades_parquet_path = trade_root / "trades.parquet"
    exit_reason_summary_path = trade_root / "exit_reason_summary.csv"
    daily_summary_path = trade_root / "daily_summary.csv"
    summary_path = trade_root / "summary.json"
    trades.to_csv(trades_csv_path, index=False)
    trades.to_parquet(trades_parquet_path, index=False)
    exit_reason_summary.to_csv(exit_reason_summary_path, index=False)
    daily_summary.to_csv(daily_summary_path, index=False)
    net_returns = pd.to_numeric(trades.get("net_return_after_cost"), errors="coerce").to_numpy(dtype=float) if len(trades) else np.asarray([], dtype=float)
    gross_returns = pd.to_numeric(trades.get("gross_return"), errors="coerce").to_numpy(dtype=float) if len(trades) else np.asarray([], dtype=float)
    payload = {
        "created_at_utc": pd.Timestamp.utcnow().isoformat(),
        "status": "completed",
        "run_dir": str(run_dir),
        "recipe_id": chosen_recipe_id,
        "recipe": recipe_payload,
        "threshold": float(chosen_threshold),
        "threshold_source": resolved_threshold_source,
        "cost_per_trade": float(utility_cfg.cost_per_trade),
        "trades": int(len(trades)),
        "long_trades": int((trades["chosen_side"] == "CE").sum()) if len(trades) else 0,
        "short_trades": int((trades["chosen_side"] == "PE").sum()) if len(trades) else 0,
        "gross_return_sum": float(np.nansum(gross_returns)) if len(trades) else 0.0,
        "net_return_sum": float(np.nansum(net_returns)) if len(trades) else 0.0,
        "gross_profit_factor": float(_profit_factor(gross_returns)),
        "net_profit_factor": float(_profit_factor(net_returns)),
        "gross_win_rate": float(np.mean(gross_returns > 0.0)) if len(trades) else 0.0,
        "net_win_rate": float(np.mean(net_returns > 0.0)) if len(trades) else 0.0,
        "time_stop_net_wins": int(((trades["exit_reason"] == "time_stop") & (trades["net_outcome"] == "win")).sum()) if len(trades) else 0,
        "time_stop_net_losses": int(((trades["exit_reason"] == "time_stop") & (trades["net_outcome"] == "loss")).sum()) if len(trades) else 0,
        "outcome_rules": {
            "tp": "gross win",
            "sl": "gross loss",
            "time_stop": "can be win or loss depending on the final horizon return; after cost, small positive moves can still become losses",
            "tp_sl_same_bar": "treated as a TP-side gross win under the current recovery research rule",
        },
        "paths": {
            "resolved_config": str(resolved_path),
            "run_summary": str(run_summary_path) if run_summary_path.exists() else None,
            "threshold_sweep_summary": str(Path(str((sweep_summary.get("paths") or {}).get("report_csv") or trade_root)).parent / "summary.json"),
            "trades_csv": str(trades_csv_path),
            "trades_parquet": str(trades_parquet_path),
            "exit_reason_summary_csv": str(exit_reason_summary_path),
            "daily_summary_csv": str(daily_summary_path),
        },
        "exit_reason_summary": exit_reason_summary.to_dict(orient="records"),
    }
    _write_json(summary_path, payload)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write a trade-by-trade report for a recovery recipe at a chosen threshold.")
    parser.add_argument("--run-dir", required=True, help="Path to a recovery run directory")
    parser.add_argument("--recipe-id", help="Recipe id under primary_recipes/. Required if the run summary is not finished yet.")
    parser.add_argument("--threshold", type=float, help="Explicit trade threshold to report")
    parser.add_argument("--threshold-source", choices=("recommended", "current"), default="recommended", help="Threshold source when --threshold is omitted")
    parser.add_argument("--output-dir", help="Optional override output directory")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = build_recovery_trade_report(
        run_dir=Path(args.run_dir),
        recipe_id=(str(args.recipe_id).strip() if args.recipe_id else None),
        threshold=(float(args.threshold) if args.threshold is not None else None),
        threshold_source=str(args.threshold_source),
        output_dir=(Path(args.output_dir) if args.output_dir else None),
    )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
