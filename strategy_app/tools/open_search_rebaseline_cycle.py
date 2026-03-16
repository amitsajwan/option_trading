"""Open-search deterministic + ML rebaseline cycle runner."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
ML_PIPELINE_SRC = REPO_ROOT / "ml_pipeline" / "src"
if ML_PIPELINE_SRC.exists():
    src_path = str(ML_PIPELINE_SRC)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

from ml_pipeline.entry_candidate_dataset import build_entry_candidate_dataset
from ml_pipeline.entry_quality_champion_select import select_champions
from ml_pipeline.entry_quality_config import (
    EntryQualityDatasetConfig,
    EntryQualitySplitConfig,
    FEATURE_PROFILES,
    LABEL_PROFILES,
    MODEL_SPECS,
    SEGMENTATION_POLICIES,
    THRESHOLD_POLICIES,
)
from ml_pipeline.entry_quality_experiments import run_experiments
from ml_pipeline.entry_quality_replay_eval import run_replay_evaluation
from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.snapshot_access import (
    DEFAULT_HISTORICAL_PARQUET_BASE,
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
from strategy_app.tools.deterministic_open_matrix import run_deterministic_open_matrix


DEFAULT_PARQUET_BASE = DEFAULT_HISTORICAL_PARQUET_BASE
DEFAULT_OUTPUT_ROOT = Path(".run/open_search_rebaseline")
DEFAULT_CAPITAL = 500000.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_list(raw: Optional[str], *, default: list[str]) -> list[str]:
    if not raw:
        return list(default)
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _stage_meta(
    *,
    command: str,
    formal_run: bool,
    manifest_meta: dict[str, Any],
    split: dict[str, str],
    gate_results: dict[str, Any],
    snapshot_access_meta: Optional[dict[str, object]] = None,
) -> dict[str, Any]:
    payload = {
        "generated_at_utc": _utc_now(),
        "command": command,
        "formal_run": bool(formal_run),
        "exploratory_only": bool(manifest_meta.get("exploratory_only", not bool(formal_run))),
        "window_manifest": manifest_meta,
        "manifest_path": manifest_meta.get("manifest_path"),
        "manifest_hash": manifest_meta.get("manifest_hash"),
        "window_start": manifest_meta.get("window_start"),
        "window_end": manifest_meta.get("window_end"),
        "split_boundaries": split,
        "gate_results": dict(gate_results),
    }
    if snapshot_access_meta:
        payload.update(snapshot_access_meta)
    return payload


def _load_valid_comparator(det_output_dir: Path) -> dict[str, Any]:
    comparator_path = det_output_dir / "valid_comparator.json"
    if not comparator_path.exists():
        raise FileNotFoundError(f"deterministic valid comparator not found: {comparator_path}")
    payload = json.loads(comparator_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("deterministic valid comparator must be a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _choose_finalists(
    valid_gate_payload: dict[str, Any],
    fallback_eval_registry: Path,
    *,
    max_finalists: int = 1,
    require_positive_return: bool = False,
) -> list[str]:
    cap = max(1, int(max_finalists))
    champions = valid_gate_payload.get("champions") or []
    finalists = [str(item.get("experiment_id")) for item in champions if isinstance(item, dict) and str(item.get("experiment_id") or "").strip()]
    if finalists:
        return finalists[:cap]

    rejected = valid_gate_payload.get("rejected_candidates") or []
    rejected_rows = [row for row in rejected if isinstance(row, dict)]
    if rejected_rows:
        rejected_df = pd.DataFrame(rejected_rows)
        if not rejected_df.empty and "experiment_id" in rejected_df.columns:
            for gate_col in (
                "min_trades_gate",
                "max_drawdown_gate",
                "drawdown_gate",
                "trade_count_gate",
                "strategy_diversification_gate",
                "return_gate",
                "positive_return_gate",
            ):
                if gate_col in rejected_df.columns:
                    rejected_df[gate_col] = rejected_df[gate_col].map(_to_bool)
                else:
                    rejected_df[gate_col] = False
            filtered = rejected_df[
                (rejected_df["min_trades_gate"] == True)
                & (rejected_df["max_drawdown_gate"] == True)
                & (rejected_df["drawdown_gate"] == True)
                & (rejected_df["trade_count_gate"] == True)
                & (rejected_df["strategy_diversification_gate"] == True)
                & (rejected_df["return_gate"] == True)
            ].copy()
            if bool(require_positive_return):
                filtered = filtered[filtered["positive_return_gate"] == True].copy()
            if not filtered.empty:
                for col in ("ml_capital_return_pct", "ml_max_drawdown_pct", "ml_profit_factor", "ml_trades"):
                    if col in filtered.columns:
                        filtered[col] = pd.to_numeric(filtered[col], errors="coerce")
                filtered = filtered.sort_values(
                    ["ml_capital_return_pct", "ml_max_drawdown_pct", "ml_profit_factor", "ml_trades"],
                    ascending=[False, False, False, False],
                    kind="stable",
                )
                ranked = [
                    str(x).strip()
                    for x in filtered["experiment_id"].tolist()
                    if str(x).strip()
                ]
                if ranked:
                    return ranked[:cap]

    eval_df = pd.read_csv(fallback_eval_registry)
    if eval_df.empty:
        return []
    if bool(require_positive_return) and "ml_capital_return_pct" in eval_df.columns:
        eval_df = eval_df[pd.to_numeric(eval_df["ml_capital_return_pct"], errors="coerce") > 0.0].copy()
        if eval_df.empty:
            return []
    eval_df = eval_df.sort_values(
        ["ml_capital_return_pct", "ml_max_drawdown_pct", "ml_profit_factor", "ml_trades"],
        ascending=[False, False, False, False],
        kind="stable",
    )
    ranked = [str(x).strip() for x in eval_df["experiment_id"].tolist() if str(x).strip()]
    return ranked[:cap]


def run_cycle(
    *,
    window_manifest: str,
    parquet_base: Path,
    output_root: Path,
    formal_run: bool,
    capital: float,
    deterministic_search_spec: Optional[str],
    feature_profiles: list[str],
    label_profiles: list[str],
    segmentation_policies: list[str],
    model_families: list[str],
    threshold_policies: list[str],
    allow_single_exception: bool,
    exception_min_trades: int,
    min_trades: int,
    max_drawdown_pct: float,
    drawdown_multiple: float,
    min_trade_ratio: float,
    max_single_strategy_return_share: float,
    max_champions: int,
    require_positive_return: bool,
    min_outperformance_pct: float,
    label_null_rate_fail_threshold: float,
    label_shift_fail_threshold: float,
    policy_diagnostic_warn_threshold: float,
    manifest_min_trading_days: int,
    manifest_required_schema_version: str,
) -> dict[str, Any]:
    manifest_meta = load_and_validate_window_manifest(
        window_manifest,
        formal_run=bool(formal_run),
        required_schema_version=str(manifest_required_schema_version),
        min_trading_days=int(manifest_min_trading_days),
        context="open_search_rebaseline_cycle.window_manifest",
    )
    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_LEGACY_RAW,
        context="open_search_rebaseline_cycle",
        parquet_base=parquet_base,
        min_day=str(manifest_meta["window_start"]),
        max_day=str(manifest_meta["window_end"]),
    )
    store = ParquetStore(parquet_base, snapshots_dataset=SNAPSHOT_DATASET_LEGACY_RAW)
    days = store.available_snapshot_days(str(manifest_meta["window_start"]), str(manifest_meta["window_end"]))
    split = split_boundaries_for_days(days)
    gate_results = {
        "formal_ready": manifest_meta.get("formal_ready"),
        "required_schema_version": str(manifest_required_schema_version),
        "min_trading_days_required": int(manifest_min_trading_days),
        "window_trading_days": manifest_meta.get("trading_days"),
        "all_days_v2": manifest_meta.get("all_days_v2"),
    }

    cycle_id = f"{manifest_meta['window_start']}_{manifest_meta['window_end']}_{str(manifest_meta.get('manifest_hash') or '')[:10]}"
    cycle_dir = output_root / cycle_id
    cycle_dir.mkdir(parents=True, exist_ok=True)
    _write_json(cycle_dir / "manifest_meta.json", manifest_meta)
    _write_json(cycle_dir / "split_boundaries.json", split)

    det_dir = cycle_dir / "deterministic"
    det_meta = _stage_meta(
        command="strategy_app.tools.deterministic_open_matrix",
        formal_run=bool(formal_run),
        manifest_meta=manifest_meta,
        split=split,
        gate_results=gate_results,
        snapshot_access_meta=snapshot_access.to_metadata(),
    )
    det_summary = run_deterministic_open_matrix(
        parquet_base=parquet_base,
        start_date=str(manifest_meta["window_start"]),
        end_date=str(manifest_meta["window_end"]),
        split_boundaries=split,
        output_dir=det_dir,
        capital=float(capital),
        search_spec_path=deterministic_search_spec,
        run_meta=det_meta,
        require_positive_return=bool(require_positive_return),
        min_outperformance_pct=float(min_outperformance_pct),
        require_accepted_holdout=bool(formal_run and require_positive_return),
    )
    valid_comparator = _load_valid_comparator(det_dir)
    deterministic_metadata = valid_comparator.get("metadata") if isinstance(valid_comparator.get("metadata"), dict) else {}
    _write_json(cycle_dir / "deterministic_valid_comparator.json", valid_comparator)

    ml_root = cycle_dir / "ml"
    ml_dataset_dir = ml_root / "candidates"
    split_cfg = EntryQualitySplitConfig(
        train_start=split["train_start"],
        train_end=split["train_end"],
        valid_start=split["valid_start"],
        valid_end=split["valid_end"],
        eval_start=split["eval_start"],
        eval_end=split["eval_end"],
    )
    dataset_cfg = EntryQualityDatasetConfig(
        snapshot_base=parquet_base,
        start_date=str(manifest_meta["window_start"]),
        end_date=str(manifest_meta["window_end"]),
        output_root=ml_dataset_dir,
        split=split_cfg,
    )
    dataset_meta = _stage_meta(
        command="ml_pipeline.entry_candidate_dataset",
        formal_run=bool(formal_run),
        manifest_meta=manifest_meta,
        split=split,
        gate_results=gate_results,
        snapshot_access_meta=snapshot_access.to_metadata(),
    )
    dataset_summary = build_entry_candidate_dataset(
        config=dataset_cfg,
        output_root=ml_dataset_dir,
        run_meta=dataset_meta,
        requested_label_columns=[
            str(LABEL_PROFILES[x].column_name)
            for x in label_profiles
            if x in LABEL_PROFILES
        ],
        label_null_rate_fail_threshold=float(label_null_rate_fail_threshold),
        label_shift_fail_threshold=float(label_shift_fail_threshold),
        policy_diagnostic_warn_threshold=float(policy_diagnostic_warn_threshold),
    )
    dataset_path = ml_dataset_dir / "entry_candidate_labels.parquet"

    experiments_dir = ml_root / "experiments"
    experiments_meta = _stage_meta(
        command="ml_pipeline.entry_quality_experiments",
        formal_run=bool(formal_run),
        manifest_meta=manifest_meta,
        split=split,
        gate_results=gate_results,
        snapshot_access_meta=snapshot_access.to_metadata(),
    )
    experiments_summary = run_experiments(
        dataset_path=dataset_path,
        output_root=experiments_dir,
        feature_profiles=feature_profiles,
        label_profiles=label_profiles,
        segmentation_policies=segmentation_policies,
        model_families=model_families,
        threshold_policies=threshold_policies,
        run_meta=experiments_meta,
    )

    valid_eval_dir = ml_root / "replay_valid"
    valid_eval_meta = _stage_meta(
        command="ml_pipeline.entry_quality_replay_eval.valid",
        formal_run=bool(formal_run),
        manifest_meta=manifest_meta,
        split=split,
        gate_results=gate_results,
        snapshot_access_meta=snapshot_access.to_metadata(),
    )
    valid_eval_summary = run_replay_evaluation(
        registry_path=experiments_dir / "experiment_registry.csv",
        parquet_base=parquet_base,
        start_date=split["valid_start"],
        end_date=split["valid_end"],
        capital=float(capital),
        output_root=valid_eval_dir,
        top_k=0,
        run_meta=valid_eval_meta,
        deterministic_metadata=deterministic_metadata,
    )

    valid_gate_dir = ml_root / "valid_gate"
    valid_eval_registry = valid_eval_dir / "evaluation_registry.csv"
    valid_eval_df = pd.read_csv(valid_eval_registry)
    valid_gate_payload = select_champions(
        evaluation_registry_path=valid_eval_registry,
        output_dir=valid_gate_dir,
        max_champions=max(1, int(len(valid_eval_df))),
        min_trades=int(min_trades),
        max_drawdown_pct=float(max_drawdown_pct),
        drawdown_multiple=float(drawdown_multiple),
        min_trade_ratio=float(min_trade_ratio),
        max_single_strategy_return_share=float(max_single_strategy_return_share),
        require_positive_return=bool(require_positive_return),
        min_outperformance_pct=float(min_outperformance_pct),
        allow_single_exception=False,
    )

    finalists = _choose_finalists(
        valid_gate_payload,
        valid_eval_registry,
        max_finalists=max_champions,
        require_positive_return=bool(require_positive_return),
    )
    if not finalists:
        raise ValueError("open-search cycle found no ML finalists from valid-stage evidence")

    full_registry = pd.read_csv(experiments_dir / "experiment_registry.csv")
    finalists_registry = full_registry[full_registry["experiment_id"].astype(str).isin(finalists)].copy()
    finalists_registry_path = ml_root / "finalists_registry.csv"
    finalists_registry.to_csv(finalists_registry_path, index=False)

    holdout_eval_dir = ml_root / "replay_holdout"
    holdout_eval_meta = _stage_meta(
        command="ml_pipeline.entry_quality_replay_eval.holdout",
        formal_run=bool(formal_run),
        manifest_meta=manifest_meta,
        split=split,
        gate_results=gate_results,
        snapshot_access_meta=snapshot_access.to_metadata(),
    )
    holdout_eval_summary = run_replay_evaluation(
        registry_path=finalists_registry_path,
        parquet_base=parquet_base,
        start_date=split["eval_start"],
        end_date=split["eval_end"],
        capital=float(capital),
        output_root=holdout_eval_dir,
        top_k=0,
        run_meta=holdout_eval_meta,
        deterministic_metadata=deterministic_metadata,
    )

    final_dir = ml_root / "champions"
    exception_reason_out = final_dir / "controlled_exception_reason.json" if allow_single_exception else None
    final_payload = select_champions(
        evaluation_registry_path=holdout_eval_dir / "evaluation_registry.csv",
        output_dir=final_dir,
        max_champions=max(1, int(max_champions)),
        min_trades=int(min_trades),
        max_drawdown_pct=float(max_drawdown_pct),
        drawdown_multiple=float(drawdown_multiple),
        min_trade_ratio=float(min_trade_ratio),
        max_single_strategy_return_share=float(max_single_strategy_return_share),
        require_positive_return=bool(require_positive_return),
        min_outperformance_pct=float(min_outperformance_pct),
        allow_single_exception=bool(allow_single_exception),
        exception_min_trades=int(exception_min_trades),
        exception_reason_out=exception_reason_out,
    )

    summary = {
        "created_at_utc": _utc_now(),
        "cycle_id": cycle_id,
        "output_dir": str(cycle_dir).replace("\\", "/"),
        "formal_run": bool(formal_run),
        "exploratory_only": bool(manifest_meta.get("exploratory_only", not bool(formal_run))),
        "manifest_hash": manifest_meta.get("manifest_hash"),
        "window_start": manifest_meta.get("window_start"),
        "window_end": manifest_meta.get("window_end"),
        "split_boundaries": split,
        "deterministic_summary": det_summary,
        "deterministic_valid_comparator_id": valid_comparator.get("candidate_id"),
        "ml_dataset_summary": dataset_summary,
        "ml_experiments_summary": experiments_summary,
        "ml_valid_evaluation_summary": valid_eval_summary,
        "ml_holdout_evaluation_summary": holdout_eval_summary,
        "ml_finalists_count": int(len(finalists)),
        "ml_final_payload_path": str((final_dir / "champion_registry.json")).replace("\\", "/"),
        "champion_count": int(len(final_payload.get("champions") or [])),
        "require_positive_return": bool(require_positive_return),
        "min_outperformance_pct": float(min_outperformance_pct),
    }
    _write_json(cycle_dir / "cycle_summary.json", summary)
    return summary


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run full open-search rebaseline cycle on one frozen window manifest.")
    parser.add_argument("--window-manifest", required=True)
    parser.add_argument("--formal-run", action="store_true")
    parser.add_argument("--parquet-base", default=str(DEFAULT_PARQUET_BASE))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--deterministic-search-spec", default=None)
    parser.add_argument("--feature-profiles", default=",".join(sorted(FEATURE_PROFILES.keys())))
    parser.add_argument("--label-profiles", default=",".join(sorted(LABEL_PROFILES.keys())))
    parser.add_argument("--segmentation-policies", default=",".join(sorted(SEGMENTATION_POLICIES.keys())))
    parser.add_argument("--model-families", default=",".join(sorted(MODEL_SPECS.keys())))
    parser.add_argument("--threshold-policies", default=",".join(sorted(THRESHOLD_POLICIES.keys())))
    parser.add_argument("--allow-single-exception", action="store_true")
    parser.add_argument("--exception-min-trades", type=int, default=30)
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--max-drawdown-pct", type=float, default=-0.50)
    parser.add_argument("--drawdown-multiple", type=float, default=1.15)
    parser.add_argument("--min-trade-ratio", type=float, default=0.60)
    parser.add_argument("--max-single-strategy-return-share", type=float, default=0.70)
    parser.add_argument("--max-champions", type=int, default=5)
    parser.add_argument("--require-positive-return", dest="require_positive_return", action="store_true")
    parser.add_argument("--allow-non-positive-return", dest="require_positive_return", action="store_false")
    parser.set_defaults(require_positive_return=None)
    parser.add_argument("--min-outperformance-pct", type=float, default=0.005)
    parser.add_argument("--label-null-rate-fail-threshold", type=float, default=0.95)
    parser.add_argument("--label-shift-fail-threshold", type=float, default=0.12)
    parser.add_argument("--policy-diagnostic-warn-threshold", type=float, default=0.30)
    parser.add_argument("--manifest-min-trading-days", type=int, default=DEFAULT_MIN_TRADING_DAYS)
    parser.add_argument("--manifest-required-schema-version", default=DEFAULT_REQUIRED_SCHEMA_VERSION)
    args = parser.parse_args(list(argv) if argv is not None else None)

    require_positive_return = (
        bool(args.formal_run) if args.require_positive_return is None else bool(args.require_positive_return)
    )
    summary = run_cycle(
        window_manifest=str(args.window_manifest),
        parquet_base=Path(args.parquet_base),
        output_root=Path(args.output_root),
        formal_run=bool(args.formal_run),
        capital=float(args.capital),
        deterministic_search_spec=args.deterministic_search_spec,
        feature_profiles=_ensure_list(args.feature_profiles, default=sorted(FEATURE_PROFILES.keys())),
        label_profiles=_ensure_list(args.label_profiles, default=sorted(LABEL_PROFILES.keys())),
        segmentation_policies=_ensure_list(args.segmentation_policies, default=sorted(SEGMENTATION_POLICIES.keys())),
        model_families=_ensure_list(args.model_families, default=sorted(MODEL_SPECS.keys())),
        threshold_policies=_ensure_list(args.threshold_policies, default=sorted(THRESHOLD_POLICIES.keys())),
        allow_single_exception=bool(args.allow_single_exception),
        exception_min_trades=int(args.exception_min_trades),
        min_trades=int(args.min_trades),
        max_drawdown_pct=float(args.max_drawdown_pct),
        drawdown_multiple=float(args.drawdown_multiple),
        min_trade_ratio=float(args.min_trade_ratio),
        max_single_strategy_return_share=float(args.max_single_strategy_return_share),
        max_champions=int(args.max_champions),
        require_positive_return=bool(require_positive_return),
        min_outperformance_pct=float(args.min_outperformance_pct),
        label_null_rate_fail_threshold=float(args.label_null_rate_fail_threshold),
        label_shift_fail_threshold=float(args.label_shift_fail_threshold),
        policy_diagnostic_warn_threshold=float(args.policy_diagnostic_warn_threshold),
        manifest_min_trading_days=int(args.manifest_min_trading_days),
        manifest_required_schema_version=str(args.manifest_required_schema_version),
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
