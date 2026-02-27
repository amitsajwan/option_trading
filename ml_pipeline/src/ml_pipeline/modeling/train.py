import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd

from ..pipeline_layout import ARTIFACTS_ROOT, ensure_layout_dirs
from .features import prepare_model_frame
from .lightgbm_trainer import (
    LABEL_TARGET_BASE,
    LABEL_TARGET_FORWARD_RETURN_THRESHOLD,
    LABEL_TARGET_PATH_TP_SL,
    LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO,
    LightGBMConfig,
    ThresholdPolicy,
    train_side,
)
from .registry import publish_model_bundle
from .regime import summarize_regimes
from .thresholds import RANKING_MODE_BREAKOUT_PF_LOGTRADES, RANKING_MODE_LEGACY


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strategy_preset_defaults(name: str) -> Dict[str, object]:
    preset = str(name).strip().lower()
    if preset == "breakout":
        return {
            "max_drawdown_pct": 0.25,
            "min_trades": 20,
            "ranking_mode": RANKING_MODE_BREAKOUT_PF_LOGTRADES,
        }
    return {
        "max_drawdown_pct": 0.15,
        "min_trades": 50,
        "ranking_mode": RANKING_MODE_LEGACY,
    }


def _load(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")
    return pd.read_parquet(path)


def run_train(
    *,
    train_path: Path,
    valid_path: Path,
    eval_path: Path,
    feature_profile: str,
    model_group: str,
    profile_id: str,
    model_root: Path,
    lgbm_cfg: LightGBMConfig,
    threshold_policy: ThresholdPolicy,
    calibration_method: str,
    side: str,
    ce_threshold_policy: Optional[ThresholdPolicy] = None,
    pe_threshold_policy: Optional[ThresholdPolicy] = None,
    label_target: str = LABEL_TARGET_BASE,
    label_horizon_minutes: Optional[int] = None,
    label_min_move_pct: Optional[float] = None,
    ce_label_min_move_pct: Optional[float] = None,
    pe_label_min_move_pct: Optional[float] = None,
) -> Dict[str, object]:
    train_raw = _load(train_path)
    valid_raw = _load(valid_path)
    eval_raw = _load(eval_path)

    train_df, feature_cols = prepare_model_frame(train_raw)
    valid_df, _ = prepare_model_frame(valid_raw)
    eval_df, _ = prepare_model_frame(eval_raw)

    train_ce = str(side).lower() in ("both", "ce")
    train_pe = str(side).lower() in ("both", "pe")
    if not (train_ce or train_pe):
        raise ValueError(f"unsupported side: {side}")

    ce_model = None
    ce_report = None
    pe_model = None
    pe_report = None
    if train_ce:
        ce_policy = ce_threshold_policy if ce_threshold_policy is not None else threshold_policy
        ce_move = float(ce_label_min_move_pct) if ce_label_min_move_pct is not None else (
            float(label_min_move_pct) if label_min_move_pct is not None else None
        )
        ce_model, ce_report = train_side(
            side="ce",
            train_df=train_df,
            valid_df=valid_df,
            eval_df=eval_df,
            feature_columns=feature_cols,
            config=lgbm_cfg,
            threshold_policy=ce_policy,
            calibration_method=calibration_method,
            label_target=label_target,
            min_move_pct=ce_move,
            label_horizon_minutes=label_horizon_minutes,
        )
    if train_pe:
        pe_policy = pe_threshold_policy if pe_threshold_policy is not None else threshold_policy
        pe_move = float(pe_label_min_move_pct) if pe_label_min_move_pct is not None else (
            float(label_min_move_pct) if label_min_move_pct is not None else None
        )
        pe_model, pe_report = train_side(
            side="pe",
            train_df=train_df,
            valid_df=valid_df,
            eval_df=eval_df,
            feature_columns=feature_cols,
            config=lgbm_cfg,
            threshold_policy=pe_policy,
            calibration_method=calibration_method,
            label_target=label_target,
            min_move_pct=pe_move,
            label_horizon_minutes=label_horizon_minutes,
        )

    report: Dict[str, object] = {
        "created_at_utc": _utc_now(),
        "model_group": str(model_group),
        "profile_id": str(profile_id),
        "feature_profile": str(feature_profile),
        "label_target": str(label_target),
        "label_horizon_minutes": (int(label_horizon_minutes) if label_horizon_minutes is not None else None),
        "label_min_move_pct": (float(label_min_move_pct) if label_min_move_pct is not None else None),
        "ce_label_min_move_pct": (float(ce_label_min_move_pct) if ce_label_min_move_pct is not None else None),
        "pe_label_min_move_pct": (float(pe_label_min_move_pct) if pe_label_min_move_pct is not None else None),
        "calibration_method": str(calibration_method),
        "trained_side": str(side).lower(),
        "rows": {"train": int(len(train_df)), "valid": int(len(valid_df)), "eval": int(len(eval_df))},
        "regime_summary": {
            "train": summarize_regimes(train_df),
            "valid": summarize_regimes(valid_df),
            "eval": summarize_regimes(eval_df),
        },
    }
    if ce_report is not None:
        report["ce"] = ce_report
    if pe_report is not None:
        report["pe"] = pe_report
    bundle: Dict[str, object] = {
        "model_type": "lightgbm_dual",
        "feature_columns": [str(c) for c in feature_cols],
        "feature_profile": str(feature_profile),
        "calibration_method": str(calibration_method),
        "trained_side": str(side).lower(),
    }
    if ce_model is not None and ce_report is not None:
        bundle["ce_model"] = ce_model
        bundle["ce_threshold"] = ce_report["threshold"]["selected_threshold"]
    if pe_model is not None and pe_report is not None:
        bundle["pe_model"] = pe_model
        bundle["pe_threshold"] = pe_report["threshold"]["selected_threshold"]
    outputs = publish_model_bundle(
        root=model_root,
        model_group=model_group,
        profile_id=profile_id,
        bundle=bundle,
        report=report,
    )
    return {"report": report, "outputs": outputs}


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Regime-aware LightGBM trainer for CE/PE.")
    parser.add_argument("--train", required=True, help="Labeled train parquet")
    parser.add_argument("--valid", required=True, help="Labeled valid parquet")
    parser.add_argument("--eval", required=True, help="Labeled eval parquet")
    parser.add_argument("--feature-profile", default="core_v2")
    parser.add_argument("--model-group", required=True, help="e.g. core_v2/h5_ts0_lgbm_regime")
    parser.add_argument("--profile-id", required=True, help="e.g. openfe_v1")
    parser.add_argument("--strategy-preset", default="default", choices=["default", "breakout"])
    parser.add_argument("--side", default="both", choices=["both", "ce", "pe"])
    parser.add_argument("--model-root", default=str((ARTIFACTS_ROOT / "models" / "by_features")).replace("\\", "/"))
    parser.add_argument("--calibration-method", default="isotonic", choices=["none", "isotonic", "platt"])
    parser.add_argument(
        "--label-target",
        default=LABEL_TARGET_BASE,
        choices=[
            LABEL_TARGET_BASE,
            LABEL_TARGET_FORWARD_RETURN_THRESHOLD,
            LABEL_TARGET_PATH_TP_SL,
            LABEL_TARGET_PATH_TP_SL_TIME_STOP_ZERO,
        ],
    )
    parser.add_argument(
        "--label-horizon-minutes",
        type=int,
        default=None,
        help="Optional horizon suffix to use from multi-horizon labels (example: 15 uses *_h15m columns).",
    )
    parser.add_argument(
        "--label-min-move-pct",
        type=float,
        default=None,
        help="Used with --label-target forward_return_threshold. Example: 0.20 means >=20%% forward move.",
    )
    parser.add_argument("--ce-label-min-move-pct", type=float, default=None)
    parser.add_argument("--pe-label-min-move-pct", type=float, default=None)
    parser.add_argument("--n-estimators", type=int, default=400)
    parser.add_argument("--learning-rate", type=float, default=0.03)
    parser.add_argument("--num-leaves", type=int, default=63)
    parser.add_argument("--max-depth", type=int, default=-1)
    parser.add_argument("--subsample", type=float, default=0.9)
    parser.add_argument("--colsample-bytree", type=float, default=0.9)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--disable-auto-scale-pos-weight", action="store_true")
    parser.add_argument("--min-scale-pos-weight", type=float, default=1.0)
    parser.add_argument("--max-scale-pos-weight", type=float, default=50.0)
    parser.add_argument("--thr-min", type=float, default=0.30)
    parser.add_argument("--thr-max", type=float, default=0.90)
    parser.add_argument("--thr-step", type=float, default=0.01)
    parser.add_argument("--cost-per-trade", type=float, default=0.0006)
    parser.add_argument("--min-profit-factor", type=float, default=1.3)
    parser.add_argument("--max-drawdown-pct", type=float, default=None)
    parser.add_argument("--min-trades", type=int, default=None)
    parser.add_argument(
        "--threshold-ranking-mode",
        default=None,
        choices=[RANKING_MODE_LEGACY, RANKING_MODE_BREAKOUT_PF_LOGTRADES],
        help="Optional override for threshold ranking behavior; defaults from --strategy-preset.",
    )
    parser.add_argument("--min-pos-rate", type=float, default=0.01)
    parser.add_argument("--max-pos-rate", type=float, default=0.20)
    parser.add_argument("--strict-pos-rate-guard", action="store_true")
    parser.add_argument("--threshold-selection", default="single", choices=["single", "walk_forward"])
    parser.add_argument("--walk-forward-folds", type=int, default=4)
    parser.add_argument("--min-fold-pass-ratio", type=float, default=0.75)
    parser.add_argument("--ce-min-profit-factor", type=float, default=None)
    parser.add_argument("--ce-max-drawdown-pct", type=float, default=None)
    parser.add_argument("--ce-min-trades", type=int, default=None)
    parser.add_argument("--ce-threshold-selection", default=None, choices=["single", "walk_forward"])
    parser.add_argument("--ce-min-fold-pass-ratio", type=float, default=None)
    parser.add_argument("--pe-min-profit-factor", type=float, default=None)
    parser.add_argument("--pe-max-drawdown-pct", type=float, default=None)
    parser.add_argument("--pe-min-trades", type=int, default=None)
    parser.add_argument("--pe-threshold-selection", default=None, choices=["single", "walk_forward"])
    parser.add_argument("--pe-min-fold-pass-ratio", type=float, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    preset_defaults = _strategy_preset_defaults(str(args.strategy_preset))

    ensure_layout_dirs()
    base_policy = ThresholdPolicy(
        min_value=float(args.thr_min),
        max_value=float(args.thr_max),
        step=float(args.thr_step),
        cost_per_trade=float(args.cost_per_trade),
        min_profit_factor=float(args.min_profit_factor),
        max_drawdown_pct=float(args.max_drawdown_pct) if args.max_drawdown_pct is not None else float(preset_defaults["max_drawdown_pct"]),
        min_trades=int(args.min_trades) if args.min_trades is not None else int(preset_defaults["min_trades"]),
        min_pos_rate=float(args.min_pos_rate),
        max_pos_rate=float(args.max_pos_rate),
        strict_pos_rate_guard=bool(args.strict_pos_rate_guard),
        selection_mode=str(args.threshold_selection),
        walk_forward_folds=int(args.walk_forward_folds),
        min_fold_pass_ratio=float(args.min_fold_pass_ratio),
        ranking_mode=str(args.threshold_ranking_mode) if args.threshold_ranking_mode else str(preset_defaults["ranking_mode"]),
    )
    ce_policy = ThresholdPolicy(
        min_value=base_policy.min_value,
        max_value=base_policy.max_value,
        step=base_policy.step,
        cost_per_trade=base_policy.cost_per_trade,
        min_profit_factor=float(args.ce_min_profit_factor) if args.ce_min_profit_factor is not None else base_policy.min_profit_factor,
        max_drawdown_pct=float(args.ce_max_drawdown_pct) if args.ce_max_drawdown_pct is not None else base_policy.max_drawdown_pct,
        min_trades=int(args.ce_min_trades) if args.ce_min_trades is not None else base_policy.min_trades,
        min_pos_rate=base_policy.min_pos_rate,
        max_pos_rate=base_policy.max_pos_rate,
        strict_pos_rate_guard=base_policy.strict_pos_rate_guard,
        selection_mode=str(args.ce_threshold_selection) if args.ce_threshold_selection else base_policy.selection_mode,
        walk_forward_folds=base_policy.walk_forward_folds,
        min_fold_pass_ratio=float(args.ce_min_fold_pass_ratio) if args.ce_min_fold_pass_ratio is not None else base_policy.min_fold_pass_ratio,
        ranking_mode=base_policy.ranking_mode,
    )
    pe_policy = ThresholdPolicy(
        min_value=base_policy.min_value,
        max_value=base_policy.max_value,
        step=base_policy.step,
        cost_per_trade=base_policy.cost_per_trade,
        min_profit_factor=float(args.pe_min_profit_factor) if args.pe_min_profit_factor is not None else base_policy.min_profit_factor,
        max_drawdown_pct=float(args.pe_max_drawdown_pct) if args.pe_max_drawdown_pct is not None else base_policy.max_drawdown_pct,
        min_trades=int(args.pe_min_trades) if args.pe_min_trades is not None else base_policy.min_trades,
        min_pos_rate=base_policy.min_pos_rate,
        max_pos_rate=base_policy.max_pos_rate,
        strict_pos_rate_guard=base_policy.strict_pos_rate_guard,
        selection_mode=str(args.pe_threshold_selection) if args.pe_threshold_selection else base_policy.selection_mode,
        walk_forward_folds=base_policy.walk_forward_folds,
        min_fold_pass_ratio=float(args.pe_min_fold_pass_ratio) if args.pe_min_fold_pass_ratio is not None else base_policy.min_fold_pass_ratio,
        ranking_mode=base_policy.ranking_mode,
    )

    out = run_train(
        train_path=Path(args.train),
        valid_path=Path(args.valid),
        eval_path=Path(args.eval),
        feature_profile=str(args.feature_profile),
        model_group=str(args.model_group),
        profile_id=str(args.profile_id),
        model_root=Path(args.model_root),
        lgbm_cfg=LightGBMConfig(
            n_estimators=int(args.n_estimators),
            learning_rate=float(args.learning_rate),
            num_leaves=int(args.num_leaves),
            max_depth=int(args.max_depth),
            subsample=float(args.subsample),
            colsample_bytree=float(args.colsample_bytree),
            random_state=int(args.random_state),
            auto_scale_pos_weight=(not bool(args.disable_auto_scale_pos_weight)),
            min_scale_pos_weight=float(args.min_scale_pos_weight),
            max_scale_pos_weight=float(args.max_scale_pos_weight),
        ),
        threshold_policy=base_policy,
        ce_threshold_policy=ce_policy,
        pe_threshold_policy=pe_policy,
        calibration_method=str(args.calibration_method),
        side=str(args.side),
        label_target=str(args.label_target),
        label_horizon_minutes=(int(args.label_horizon_minutes) if args.label_horizon_minutes is not None else None),
        label_min_move_pct=(float(args.label_min_move_pct) if args.label_min_move_pct is not None else None),
        ce_label_min_move_pct=(float(args.ce_label_min_move_pct) if args.ce_label_min_move_pct is not None else None),
        pe_label_min_move_pct=(float(args.pe_label_min_move_pct) if args.pe_label_min_move_pct is not None else None),
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
