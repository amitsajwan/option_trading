import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .config import LabelConfig
from .dataset_builder import build_canonical_dataset
from .feature.engineering import build_feature_table
from .label_engine import EffectiveLabelConfig, build_labeled_dataset
from .pipeline_layout import ensure_layout_dirs, resolve_market_archive_base, resolve_vix_source
from .schema_validator import discover_available_days
from .temporal_splits import partition_days_with_reserve
from .training_cycle import (
    LABEL_TARGET_BASE,
    LABEL_TARGET_PATH_TP_SL,
    LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO,
    PreprocessConfig,
    TradingObjectiveConfig,
    run_training_cycle,
)


IST = timezone(timedelta(hours=5, minutes=30))


def _chunk_days(days: Sequence[str], chunk_size_days: int) -> List[List[str]]:
    chunk = max(1, int(chunk_size_days))
    out: List[List[str]] = []
    cur: List[str] = []
    for day in days:
        cur.append(str(day))
        if len(cur) >= chunk:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _window_days(days: Sequence[str], lookback_years: int, end_day: Optional[str]) -> List[str]:
    if not days:
        return []
    unique = sorted({str(day) for day in days})
    last_day = pd.Timestamp(end_day) if end_day else pd.Timestamp(unique[-1])
    start_day = (last_day - pd.DateOffset(years=int(lookback_years))) + pd.Timedelta(days=1)
    return [day for day in unique if start_day <= pd.Timestamp(day) <= last_day]


def _effective_label_cfg(
    *,
    horizon_minutes: int,
    stop_loss_pct: float,
    take_profit_pct: float,
    allow_hold_extension: bool = False,
    extension_trigger_profit_pct: float = 0.0,
) -> EffectiveLabelConfig:
    label_cfg = LabelConfig()
    return EffectiveLabelConfig(
        horizon_minutes=int(horizon_minutes),
        return_threshold=float(label_cfg.return_threshold),
        use_excursion_gate=bool(label_cfg.use_excursion_gate),
        min_favorable_excursion=float(label_cfg.min_favorable_excursion),
        max_adverse_excursion=float(label_cfg.max_adverse_excursion),
        stop_loss_pct=float(stop_loss_pct),
        take_profit_pct=float(take_profit_pct),
        allow_hold_extension=bool(allow_hold_extension),
        extension_trigger_profit_pct=float(extension_trigger_profit_pct),
    )


def run_two_year_pipeline(
    *,
    base_path: Path,
    lookback_years: int,
    end_day: Optional[str],
    reserve_months: int,
    vix_path: Optional[str],
    chunk_size_days: int,
    artifact_prefix: str,
    objective: str,
    feature_profile: str,
    label_target: str,
    label_horizon_minutes: int,
    label_stop_loss_pct: float,
    label_take_profit_pct: float,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    purge_days: int,
    embargo_days: int,
    random_state: int,
    max_experiments: Optional[int],
    preprocess_cfg: PreprocessConfig,
    utility_cfg: TradingObjectiveConfig,
    artifacts_root: Optional[Path] = None,
    reuse_artifacts: bool = True,
) -> Dict[str, object]:
    available = discover_available_days(base_path)
    partition = partition_days_with_reserve(
        available,
        lookback_years=lookback_years,
        evaluation_end_day=end_day,
        reserve_months=reserve_months,
    )
    model_days = list(partition.model_days)
    holdout_days = list(partition.holdout_days)
    if not model_days:
        raise ValueError("no model days in requested window")

    artifacts_dir = Path(artifacts_root) if artifacts_root is not None else Path("ml_pipeline/artifacts")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    panel_path = artifacts_dir / f"{artifact_prefix}_t03_canonical_panel.parquet"
    features_path = artifacts_dir / f"{artifact_prefix}_t04_features.parquet"
    labeled_path = artifacts_dir / f"{artifact_prefix}_t05_labeled_features.parquet"
    report_path = artifacts_dir / f"{artifact_prefix}_training_cycle_report.json"
    model_path = artifacts_dir / f"{artifact_prefix}_best_model.joblib"
    summary_path = artifacts_dir / f"{artifact_prefix}_run_summary.json"
    holdout_days_path = artifacts_dir / f"{artifact_prefix}_holdout_days.json"

    chunks = _chunk_days(model_days, chunk_size_days=chunk_size_days)
    panel_parts: List[pd.DataFrame] = []
    chunk_reports: List[Dict[str, object]] = []
    if reuse_artifacts and panel_path.exists():
        panel = pd.read_parquet(panel_path)
        for idx, day_chunk in enumerate(chunks, start=1):
            chunk_reports.append(
                {
                    "chunk_index": int(idx),
                    "chunk_days": int(len(day_chunk)),
                    "start_day": str(day_chunk[0]),
                    "end_day": str(day_chunk[-1]),
                    "rows": None,
                    "source": "reused_panel",
                }
            )
    else:
        for idx, day_chunk in enumerate(chunks, start=1):
            chunk_path = artifacts_dir / f"{artifact_prefix}_panel_chunk_{idx:02d}.parquet"
            if reuse_artifacts and chunk_path.exists():
                panel_chunk = pd.read_parquet(chunk_path)
                source = "reused_chunk"
            else:
                panel_chunk = build_canonical_dataset(base_path=base_path, days=day_chunk)
                panel_chunk.to_parquet(chunk_path, index=False)
                source = "built_chunk"
            panel_parts.append(panel_chunk)
            chunk_reports.append(
                {
                    "chunk_index": int(idx),
                    "chunk_days": int(len(day_chunk)),
                    "start_day": str(day_chunk[0]),
                    "end_day": str(day_chunk[-1]),
                    "rows": int(len(panel_chunk)),
                    "source": source,
                    "chunk_artifact": str(chunk_path),
                }
            )
        panel = pd.concat(panel_parts, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
        panel.to_parquet(panel_path, index=False)

    if reuse_artifacts and features_path.exists():
        features = pd.read_parquet(features_path)
    else:
        features = build_feature_table(panel, vix_source=vix_path)
        features.to_parquet(features_path, index=False)

    if reuse_artifacts and labeled_path.exists():
        labeled = pd.read_parquet(labeled_path)
    else:
        labeled = build_labeled_dataset(
            features=features,
            base_path=base_path,
            cfg=_effective_label_cfg(
                horizon_minutes=label_horizon_minutes,
                stop_loss_pct=label_stop_loss_pct,
                take_profit_pct=label_take_profit_pct,
                allow_hold_extension=False,
                extension_trigger_profit_pct=0.0,
            ),
        )
        labeled.to_parquet(labeled_path, index=False)

    cycle_out = run_training_cycle(
        labeled_df=labeled,
        feature_profile=feature_profile,
        objective=objective,
        label_target=label_target,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        purge_days=purge_days,
        embargo_days=embargo_days,
        random_state=random_state,
        max_experiments=max_experiments,
        preprocess_cfg=preprocess_cfg,
        utility_cfg=utility_cfg,
    )

    created_at = datetime.now(IST).isoformat()
    paths = {
        "panel": str(panel_path),
        "features": str(features_path),
        "labeled": str(labeled_path),
        "training_report": str(report_path),
        "model_package": str(model_path),
        "run_summary": str(summary_path),
        "holdout_days": str(holdout_days_path),
    }
    report_path.write_text(json.dumps(cycle_out["report"], indent=2), encoding="utf-8")
    holdout_days_path.write_text(
        json.dumps(
            {
                "evaluation_end_day": str(partition.evaluation_end_day),
                "reserve_months": int(reserve_months),
                "holdout_days_total": int(len(holdout_days)),
                "holdout_days": holdout_days,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    import joblib

    joblib.dump(cycle_out["model_package"], model_path)

    run_summary = {
        "created_at_ist": created_at,
        "base_path": str(base_path),
        "vix_path": (str(vix_path) if vix_path else None),
        "lookback_years": int(lookback_years),
        "evaluation_end_day": str(partition.evaluation_end_day),
        "window_start": str(partition.model_window_start),
        "window_end": str(partition.model_window_end),
        "window_days": int(len(model_days)),
        "reserve_months": int(reserve_months),
        "holdout_days_total": int(len(holdout_days)),
        "holdout_start": (str(holdout_days[0]) if holdout_days else None),
        "holdout_end": (str(holdout_days[-1]) if holdout_days else None),
        "chunks": chunk_reports,
        "rows": {
            "panel": int(len(panel)),
            "features": int(len(features)),
            "labeled": int(len(labeled)),
        },
        "objective": str(objective),
        "feature_profile": str(feature_profile),
        "label_target": str(label_target),
        "label_horizon_minutes": int(label_horizon_minutes),
        "label_stop_loss_pct": float(label_stop_loss_pct),
        "label_take_profit_pct": float(label_take_profit_pct),
        "best_experiment": cycle_out["report"]["best_experiment"]["experiment_id"],
        "best_objective_value": cycle_out["report"]["best_experiment"]["objective_value"],
        "artifacts": paths,
    }
    summary_path.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    return run_summary


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="End-to-end 2-year training pipeline on real historical data")
    parser.add_argument("--base-path", default=None, help="Archive base path")
    parser.add_argument("--lookback-years", type=int, default=2)
    parser.add_argument("--end-day", default=None, help="Evaluation end day YYYY-MM-DD (default: auto based on reserve)")
    parser.add_argument("--reserve-months", type=int, default=3, help="Reserve recent months as strict unseen holdout")
    parser.add_argument("--vix-path", default=None, help="Optional VIX file/dir for regime features")
    parser.add_argument("--chunk-size-days", type=int, default=70)
    parser.add_argument("--artifact-prefix", default="t29_2y")
    parser.add_argument(
        "--objective",
        default="trade_utility",
        choices=["rmse", "brier", "f1", "roc_auc", "pr_auc", "accuracy", "precision", "recall", "trade_utility"],
    )
    parser.add_argument("--feature-profile", default="futures_options_only")
    parser.add_argument(
        "--label-target",
        default=LABEL_TARGET_PATH_TP_SL,
        choices=[LABEL_TARGET_PATH_TP_SL, LABEL_TARGET_BASE, LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO],
    )
    parser.add_argument("--label-horizon-minutes", type=int, default=15)
    parser.add_argument("--label-stop-loss-pct", type=float, default=0.20)
    parser.add_argument("--label-take-profit-pct", type=float, default=0.30)
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--valid-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument("--purge-days", type=int, default=0)
    parser.add_argument("--embargo-days", type=int, default=0)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--max-experiments", type=int, default=18)
    parser.add_argument("--max-missing-rate", type=float, default=0.35)
    parser.add_argument("--clip-lower-q", type=float, default=0.01)
    parser.add_argument("--clip-upper-q", type=float, default=0.99)
    parser.add_argument("--utility-ce-threshold", type=float, default=0.60)
    parser.add_argument("--utility-pe-threshold", type=float, default=0.60)
    parser.add_argument("--utility-cost-per-trade", type=float, default=0.0006)
    parser.add_argument("--utility-min-profit-factor", type=float, default=1.30)
    parser.add_argument("--utility-max-equity-drawdown-pct", type=float, default=0.15)
    parser.add_argument("--utility-max-abs-drawdown", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--utility-min-trades", type=int, default=50)
    parser.add_argument("--utility-risk-per-trade-pct", type=float, default=0.01)
    parser.add_argument("--utility-keep-time-stop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--utility-discard-time-stop", action="store_true")
    parser.add_argument("--no-reuse-artifacts", action="store_true", help="Disable reuse/resume from existing artifacts")
    args = parser.parse_args(list(argv) if argv is not None else None)

    ensure_layout_dirs()
    base = resolve_market_archive_base(explicit_base=args.base_path)
    if base is None:
        print("ERROR: archive base path not found")
        return 2
    vix_source = resolve_vix_source(explicit_vix=args.vix_path)

    summary = run_two_year_pipeline(
        base_path=base,
        lookback_years=int(args.lookback_years),
        end_day=args.end_day,
        reserve_months=int(args.reserve_months),
        vix_path=vix_source,
        chunk_size_days=int(args.chunk_size_days),
        artifact_prefix=str(args.artifact_prefix),
        objective=str(args.objective),
        feature_profile=str(args.feature_profile),
        label_target=str(args.label_target),
        label_horizon_minutes=int(args.label_horizon_minutes),
        label_stop_loss_pct=float(args.label_stop_loss_pct),
        label_take_profit_pct=float(args.label_take_profit_pct),
        train_days=int(args.train_days),
        valid_days=int(args.valid_days),
        test_days=int(args.test_days),
        step_days=int(args.step_days),
        purge_days=int(args.purge_days),
        embargo_days=int(args.embargo_days),
        random_state=int(args.random_state),
        max_experiments=int(args.max_experiments) if args.max_experiments is not None else None,
        preprocess_cfg=PreprocessConfig(
            max_missing_rate=float(args.max_missing_rate),
            clip_lower_q=float(args.clip_lower_q),
            clip_upper_q=float(args.clip_upper_q),
        ),
        utility_cfg=TradingObjectiveConfig(
            ce_threshold=float(args.utility_ce_threshold),
            pe_threshold=float(args.utility_pe_threshold),
            cost_per_trade=float(args.utility_cost_per_trade),
            min_profit_factor=float(args.utility_min_profit_factor),
            max_equity_drawdown_pct=(
                float(args.utility_max_abs_drawdown)
                if args.utility_max_abs_drawdown is not None
                else float(args.utility_max_equity_drawdown_pct)
            ),
            min_trades=int(args.utility_min_trades),
            take_profit_pct=float(args.label_take_profit_pct),
            stop_loss_pct=float(args.label_stop_loss_pct),
            discard_time_stop=bool(args.utility_discard_time_stop) and (not bool(args.utility_keep_time_stop)),
            risk_per_trade_pct=float(args.utility_risk_per_trade_pct),
        ),
        reuse_artifacts=not bool(args.no_reuse_artifacts),
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())

