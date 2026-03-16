from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.snapshot_access import (
    SNAPSHOT_DATASET_LEGACY_RAW,
    SNAPSHOT_INPUT_MODE_LEGACY_RAW,
    require_snapshot_access,
)
from snapshot_app.historical.window_manifest import (
    DEFAULT_MIN_TRADING_DAYS,
    DEFAULT_REQUIRED_SCHEMA_VERSION,
    load_and_validate_window_manifest,
    split_boundaries_for_days,
)
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.engines.ml_entry_policy import MLEntryPolicy
from strategy_app.tools.offline_strategy_analysis import MemorySignalLogger, _group_table, _summary

from .entry_quality_config import (
    DEFAULT_CONTINUOUS_END,
    DEFAULT_CONTINUOUS_START,
    EntryQualityDatasetConfig,
    DEFAULT_MODEL_ROOT,
    THRESHOLD_FIXED_060,
    THRESHOLD_FIXED_065,
    THRESHOLD_POLICIES,
    THRESHOLD_SEGMENT_OPTIMAL,
    THRESHOLD_STRATEGY_OVERRIDE,
    threshold_policy_for_id,
)
from .snapshot_quality_gate import REQUIRED_SNAPSHOT_SCHEMA_VERSION, enforce_snapshot_schema_version

DEFAULT_CAPITAL = 500000.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_custom_fixed_threshold_policy(policy_id: str) -> Optional[float]:
    text = str(policy_id or "").strip().lower()
    prefix = "fixed_custom_"
    if not text.startswith(prefix):
        return None
    raw = text[len(prefix) :].strip()
    if not raw:
        return None
    value: Optional[float]
    if raw.replace(".", "", 1).isdigit():
        if "." in raw:
            try:
                value = float(raw)
            except Exception:
                return None
        else:
            try:
                number = int(raw)
            except Exception:
                return None
            value = float(number) / 100.0
    else:
        return None
    if value is None:
        return None
    if value <= 0.0:
        return None
    if value >= 1.0:
        value = value / 100.0
    if value <= 0.0 or value >= 1.0:
        return None
    return float(value)


def _load_snapshots(parquet_base: Path, start_date: str, end_date: str) -> pd.DataFrame:
    require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_LEGACY_RAW,
        context="entry_quality_replay_eval",
        parquet_base=parquet_base,
        min_day=start_date,
        max_day=end_date,
    )
    store = ParquetStore(parquet_base, snapshots_dataset=SNAPSHOT_DATASET_LEGACY_RAW)
    df = store.snapshots_for_date_range(start_date, end_date)
    df = enforce_snapshot_schema_version(
        df,
        required_version=REQUIRED_SNAPSHOT_SCHEMA_VERSION,
        context=f"entry_quality_replay_eval[{start_date}..{end_date}]",
    )
    if df.empty:
        return df
    return df.loc[:, ["trade_date", "timestamp", "snapshot_raw_json"]].copy()


def _resolve_threshold_kwargs(bundle_path: str | Path, threshold_policy_id: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model_package_path": str(bundle_path),
    }
    custom_fixed = _parse_custom_fixed_threshold_policy(threshold_policy_id)
    if custom_fixed is not None:
        kwargs["default_threshold"] = float(custom_fixed)
        return kwargs
    policy = threshold_policy_for_id(threshold_policy_id)
    if policy.policy_id in {THRESHOLD_FIXED_060, THRESHOLD_FIXED_065}:
        kwargs["default_threshold"] = float(policy.default_threshold)
    elif policy.policy_id == THRESHOLD_STRATEGY_OVERRIDE:
        kwargs["strategy_threshold_overrides"] = dict(policy.strategy_overrides)
    elif policy.policy_id == THRESHOLD_SEGMENT_OPTIMAL:
        pass
    else:
        if policy.default_threshold is not None:
            kwargs["default_threshold"] = float(policy.default_threshold)
        if policy.strategy_overrides:
            kwargs["strategy_threshold_overrides"] = dict(policy.strategy_overrides)
    if policy.strategy_regime_overrides:
        kwargs["strategy_regime_threshold_overrides"] = dict(policy.strategy_regime_overrides)
    return kwargs


def _select_top_candidates(registry: pd.DataFrame, top_k: int) -> pd.DataFrame:
    trained = registry[registry["status"] == "trained"].copy()
    if trained.empty:
        return trained

    sort_columns: list[str] = []
    ascending: list[bool] = []
    for column, direction in [
        ("offline_rank_score", False),
        ("offline_eval_pr_auc", False),
        ("offline_eval_roc_auc", False),
        ("offline_eval_brier", True),
    ]:
        if column in trained.columns:
            trained[column] = pd.to_numeric(trained[column], errors="coerce")
            if trained[column].notna().any():
                sort_columns.append(column)
                ascending.append(direction is True)

    if sort_columns:
        trained = trained.sort_values(sort_columns, ascending=ascending, kind="stable")
    if int(top_k) <= 0:
        return trained.copy()
    return trained.head(int(top_k)).copy()


def _run_replay(
    *,
    snapshots: pd.DataFrame,
    capital_allocated: float,
    bundle_path: Optional[Path] = None,
    threshold_policy_id: Optional[str] = None,
    deterministic_metadata: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    logger = MemorySignalLogger(capital_allocated=capital_allocated)
    entry_policy = None
    if bundle_path is not None:
        kwargs = _resolve_threshold_kwargs(bundle_path, threshold_policy_id or THRESHOLD_SEGMENT_OPTIMAL)
        entry_policy = MLEntryPolicy(**kwargs)
    engine = DeterministicRuleEngine(signal_logger=logger, entry_policy=entry_policy)
    run_metadata = dict(deterministic_metadata or {})
    if not isinstance(run_metadata.get("risk_config"), dict):
        run_metadata["risk_config"] = {}
    engine.set_run_context(
        f"entry-quality-replay-{threshold_policy_id or 'deterministic'}",
        run_metadata,
    )

    current_day: Optional[str] = None
    for row in snapshots.itertuples(index=False):
        trade_day = str(row.trade_date)
        if trade_day != current_day:
            if current_day is not None:
                engine.on_session_end(date.fromisoformat(current_day))
            engine.on_session_start(date.fromisoformat(trade_day))
            current_day = trade_day
        payload = json.loads(str(row.snapshot_raw_json))
        engine.evaluate(payload)
    if current_day is not None:
        engine.on_session_end(date.fromisoformat(current_day))

    df = pd.DataFrame(logger.trades)
    if df.empty:
        return df
    df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
    df["exit_time"] = pd.to_datetime(df["exit_time"], errors="coerce")
    return df.sort_values(["exit_time", "position_id"], kind="stable").reset_index(drop=True)


def evaluate_experiment(
    *,
    snapshots: pd.DataFrame,
    start_date: str,
    end_date: str,
    capital: float,
    experiment_row: dict[str, Any],
    output_dir: Path,
    deterministic_df: Optional[pd.DataFrame] = None,
    deterministic_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if deterministic_df is None:
        deterministic_df = _run_replay(
            snapshots=snapshots,
            capital_allocated=capital,
            deterministic_metadata=deterministic_metadata,
        )
    ml_df = _run_replay(
        snapshots=snapshots,
        capital_allocated=capital,
        bundle_path=Path(str(experiment_row["bundle_path"])),
        threshold_policy_id=str(experiment_row["threshold_policy_id"]),
        deterministic_metadata=deterministic_metadata,
    )

    deterministic_summary = _summary(deterministic_df, capital_allocated=capital)
    ml_summary = _summary(ml_df, capital_allocated=capital)

    comparison = pd.DataFrame(
        [
            {"mode": "deterministic", **deterministic_summary},
            {"mode": "ml", **ml_summary},
        ]
    )
    comparison.to_csv(output_dir / "comparison.csv", index=False)
    deterministic_df.to_csv(output_dir / "deterministic_trades.csv", index=False)
    ml_df.to_csv(output_dir / "ml_trades.csv", index=False)
    _group_table(deterministic_df, ["entry_strategy"]).to_csv(output_dir / "deterministic_by_strategy.csv", index=False)
    _group_table(ml_df, ["entry_strategy"]).to_csv(output_dir / "ml_by_strategy.csv", index=False)
    _group_table(ml_df, ["regime"]).to_csv(output_dir / "ml_by_regime.csv", index=False)
    _group_table(ml_df, ["entry_strategy", "regime"]).to_csv(output_dir / "ml_by_strategy_regime.csv", index=False)

    summary = {
        "created_at_utc": _utc_now(),
        "experiment_id": experiment_row["experiment_id"],
        "base_experiment_key": experiment_row["base_experiment_key"],
        "bundle_path": experiment_row["bundle_path"],
        "threshold_policy_id": experiment_row["threshold_policy_id"],
        "start_date": start_date,
        "end_date": end_date,
        "capital": capital,
        "deterministic": deterministic_summary,
        "ml": ml_summary,
        "outputs": {
            "comparison_csv": str((output_dir / "comparison.csv")).replace("\\", "/"),
            "deterministic_trades_csv": str((output_dir / "deterministic_trades.csv")).replace("\\", "/"),
            "ml_trades_csv": str((output_dir / "ml_trades.csv")).replace("\\", "/"),
            "deterministic_by_strategy_csv": str((output_dir / "deterministic_by_strategy.csv")).replace("\\", "/"),
            "ml_by_strategy_csv": str((output_dir / "ml_by_strategy.csv")).replace("\\", "/"),
            "ml_by_regime_csv": str((output_dir / "ml_by_regime.csv")).replace("\\", "/"),
            "ml_by_strategy_regime_csv": str((output_dir / "ml_by_strategy_regime.csv")).replace("\\", "/"),
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_replay_evaluation(
    *,
    registry_path: Path,
    parquet_base: Path,
    start_date: str,
    end_date: str,
    capital: float,
    output_root: Path,
    top_k: int = 10,
    run_meta: Optional[dict[str, Any]] = None,
    deterministic_metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    registry = pd.read_csv(registry_path)
    selected_df = _select_top_candidates(registry, top_k)
    if selected_df.empty:
        raise ValueError("no trained experiments found in registry")
    selected = selected_df.to_dict(orient="records")
    output_root.mkdir(parents=True, exist_ok=True)
    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_LEGACY_RAW,
        context="entry_quality_replay_eval",
        parquet_base=parquet_base,
        min_day=start_date,
        max_day=end_date,
    )
    snapshots = _load_snapshots(parquet_base, start_date, end_date)
    deterministic_df = _run_replay(
        snapshots=snapshots,
        capital_allocated=capital,
        deterministic_metadata=deterministic_metadata,
    )
    eval_rows: list[dict[str, Any]] = []
    for row in selected:
        exp_dir = output_root / str(row["experiment_id"])
        exp_dir.mkdir(parents=True, exist_ok=True)
        result = evaluate_experiment(
            snapshots=snapshots,
            start_date=start_date,
            end_date=end_date,
            capital=capital,
            experiment_row=row,
            output_dir=exp_dir,
            deterministic_df=deterministic_df.copy(),
            deterministic_metadata=deterministic_metadata,
        )
        eval_rows.append(
            {
                "experiment_id": result["experiment_id"],
                "base_experiment_key": result["base_experiment_key"],
                "threshold_policy_id": result["threshold_policy_id"],
                "ml_capital_return_pct": result["ml"]["net_capital_return_pct"],
                "ml_max_drawdown_pct": result["ml"]["max_drawdown_pct"],
                "ml_profit_factor": result["ml"]["profit_factor"],
                "ml_trades": result["ml"]["trades"],
                "det_capital_return_pct": result["deterministic"]["net_capital_return_pct"],
                "det_max_drawdown_pct": result["deterministic"]["max_drawdown_pct"],
                "det_trades": result["deterministic"]["trades"],
                "summary_json": str((exp_dir / "summary.json")).replace("\\", "/"),
            }
        )
    eval_df = pd.DataFrame(eval_rows).sort_values(
        ["ml_capital_return_pct", "ml_max_drawdown_pct", "ml_profit_factor"],
        ascending=[False, False, False],
        kind="stable",
    )
    eval_df.to_csv(output_root / "evaluation_registry.csv", index=False)
    summary = {
        "created_at_utc": _utc_now(),
        "registry_path": str(registry_path).replace("\\", "/"),
        "start_date": start_date,
        "end_date": end_date,
        "required_snapshot_schema_version": REQUIRED_SNAPSHOT_SCHEMA_VERSION,
        **snapshot_access.to_metadata(),
        "capital": capital,
        "top_k_requested": int(top_k),
        "selected_experiments": int(len(selected_df)),
        "evaluated_experiments": int(len(eval_df)),
        "evaluation_registry_csv": str((output_root / "evaluation_registry.csv")).replace("\\", "/"),
    }
    (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if run_meta is not None:
        (output_root / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
    return summary


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay-evaluate canonical entry-quality experiment candidates.")
    parser.add_argument("--registry", default=str(DEFAULT_MODEL_ROOT / "entry_quality_experiments" / "experiment_registry.csv"))
    parser.add_argument("--parquet-base", default=str(EntryQualityDatasetConfig().snapshot_base))
    parser.add_argument("--start-date", default=DEFAULT_CONTINUOUS_START)
    parser.add_argument("--end-date", default=DEFAULT_CONTINUOUS_END)
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--output-dir", default=str(DEFAULT_MODEL_ROOT / "entry_quality_replay_eval"))
    parser.add_argument("--top-k", type=int, default=0)
    parser.add_argument(
        "--deterministic-config-json",
        default=None,
        help="Optional JSON file with deterministic run metadata (risk/regime/router config) used as replay comparator.",
    )
    parser.add_argument("--window-manifest", default=None, help="Path to canonical window manifest JSON.")
    parser.add_argument("--formal-run", action="store_true", help="Enforce formal readiness rules from window manifest.")
    parser.add_argument("--manifest-min-trading-days", type=int, default=DEFAULT_MIN_TRADING_DAYS)
    parser.add_argument("--manifest-required-schema-version", default=DEFAULT_REQUIRED_SCHEMA_VERSION)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.formal_run and not args.window_manifest:
        raise SystemExit("--formal-run requires --window-manifest")

    manifest_meta: Optional[dict[str, Any]] = None
    start_date = str(args.start_date)
    end_date = str(args.end_date)
    split_bounds: Optional[dict[str, str]] = None
    if args.window_manifest:
        manifest_meta = load_and_validate_window_manifest(
            args.window_manifest,
            formal_run=bool(args.formal_run),
            required_schema_version=str(args.manifest_required_schema_version),
            min_trading_days=int(args.manifest_min_trading_days),
            context="entry_quality_replay_eval.window_manifest",
        )
        start_date = str(manifest_meta["window_start"])
        end_date = str(manifest_meta["window_end"])
        try:
            store = ParquetStore(Path(args.parquet_base), snapshots_dataset=SNAPSHOT_DATASET_LEGACY_RAW)
            split_days = store.available_snapshot_days(start_date, end_date)
            split_bounds = split_boundaries_for_days(split_days)
        except Exception:
            split_bounds = None

    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_LEGACY_RAW,
        context="entry_quality_replay_eval",
        parquet_base=Path(args.parquet_base),
        min_day=start_date,
        max_day=end_date,
    )

    deterministic_metadata: Optional[dict[str, Any]] = None
    if args.deterministic_config_json:
        deterministic_metadata = json.loads(Path(args.deterministic_config_json).read_text(encoding="utf-8"))
        if not isinstance(deterministic_metadata, dict):
            raise SystemExit("--deterministic-config-json must contain a JSON object")

    run_meta = {
        "generated_at_utc": _utc_now(),
        "command": "strategy_app.offline_ml.entry_quality_replay_eval",
        "formal_run": bool(args.formal_run),
        "exploratory_only": bool((manifest_meta or {}).get("exploratory_only", not bool(args.formal_run))),
        "window_manifest": manifest_meta,
        "manifest_path": (manifest_meta or {}).get("manifest_path"),
        "manifest_hash": (manifest_meta or {}).get("manifest_hash"),
        "window_start": start_date,
        "window_end": end_date,
        **snapshot_access.to_metadata(),
        "split_boundaries": split_bounds,
        "deterministic_config_json": (
            str(Path(args.deterministic_config_json).resolve()) if args.deterministic_config_json else None
        ),
        "gate_results": {
            "formal_ready": (manifest_meta or {}).get("formal_ready"),
            "required_schema_version": str(args.manifest_required_schema_version),
            "min_trading_days_required": int(args.manifest_min_trading_days),
            "window_trading_days": (manifest_meta or {}).get("trading_days"),
            "all_days_v2": (manifest_meta or {}).get("all_days_v2"),
        },
    }

    try:
        summary = run_replay_evaluation(
            registry_path=Path(args.registry),
            parquet_base=Path(args.parquet_base),
            start_date=start_date,
            end_date=end_date,
            capital=float(args.capital),
            output_root=Path(args.output_dir),
            top_k=int(args.top_k),
            run_meta=run_meta,
            deterministic_metadata=deterministic_metadata,
        )
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
