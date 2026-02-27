import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..live_inference_adapter import load_model_package, load_thresholds, predict_probabilities_from_frame
from ..modeling.metrics import classification_metrics, threshold_stats


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")
    frame = pd.read_parquet(path)
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    return frame


def _as_day_series(frame: pd.DataFrame) -> pd.Series:
    if "trade_date" in frame.columns:
        return pd.to_datetime(frame["trade_date"], errors="coerce").dt.date.astype("object")
    if "timestamp" in frame.columns:
        return pd.to_datetime(frame["timestamp"], errors="coerce").dt.date.astype("object")
    return pd.Series([None] * len(frame), index=frame.index, dtype="object")


def _evaluate_side(
    *,
    frame: pd.DataFrame,
    prob: np.ndarray,
    side: str,
    threshold: float,
    cost_per_trade: float,
) -> Dict[str, object]:
    label_col = f"{side}_label"
    valid_col = f"{side}_label_valid"
    ret_col = f"{side}_forward_return"
    if label_col not in frame.columns or valid_col not in frame.columns or ret_col not in frame.columns:
        return {
            "available": False,
            "reason": f"missing columns for {side}: {label_col}/{valid_col}/{ret_col}",
        }

    valid_mask = pd.to_numeric(frame[valid_col], errors="coerce").fillna(0.0) == 1.0
    labeled_mask = frame[label_col].notna()
    mask = (valid_mask & labeled_mask).to_numpy()
    if int(np.sum(mask)) == 0:
        return {"available": False, "reason": f"no valid rows for {side}"}

    y = pd.to_numeric(frame.loc[mask, label_col], errors="coerce").fillna(0.0).astype(int).to_numpy()
    p = np.asarray(prob, dtype=float)[mask]
    r = pd.to_numeric(frame.loc[mask, ret_col], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    return {
        "available": True,
        "rows": int(len(y)),
        "classification": classification_metrics(y, p),
        "threshold": threshold_stats(p, r, float(threshold), float(cost_per_trade)),
    }


def _build_dual_actions(
    *,
    ce_prob: np.ndarray,
    pe_prob: np.ndarray,
    ce_threshold: float,
    pe_threshold: float,
) -> np.ndarray:
    ce_ok = np.asarray(ce_prob, dtype=float) >= float(ce_threshold)
    pe_ok = np.asarray(pe_prob, dtype=float) >= float(pe_threshold)
    actions = np.full(shape=(len(ce_ok),), fill_value="HOLD", dtype=object)
    ce_only = ce_ok & (~pe_ok)
    pe_only = pe_ok & (~ce_ok)
    both = ce_ok & pe_ok
    actions[ce_only] = "BUY_CE"
    actions[pe_only] = "BUY_PE"
    actions[both] = np.where(np.asarray(ce_prob, dtype=float)[both] >= np.asarray(pe_prob, dtype=float)[both], "BUY_CE", "BUY_PE")
    return actions


def _compute_dual_summary(
    *,
    frame: pd.DataFrame,
    probs: pd.DataFrame,
    ce_threshold: float,
    pe_threshold: float,
    cost_per_trade: float,
) -> Dict[str, object]:
    work = frame.copy()
    work["ce_prob"] = pd.to_numeric(probs["ce_prob"], errors="coerce").to_numpy(dtype=float)
    work["pe_prob"] = pd.to_numeric(probs["pe_prob"], errors="coerce").to_numpy(dtype=float)
    work["action"] = _build_dual_actions(
        ce_prob=work["ce_prob"].to_numpy(dtype=float),
        pe_prob=work["pe_prob"].to_numpy(dtype=float),
        ce_threshold=float(ce_threshold),
        pe_threshold=float(pe_threshold),
    )

    ce_valid = pd.to_numeric(work.get("ce_label_valid"), errors="coerce").fillna(0.0) == 1.0
    pe_valid = pd.to_numeric(work.get("pe_label_valid"), errors="coerce").fillna(0.0) == 1.0
    ce_ret = pd.to_numeric(work.get("ce_forward_return"), errors="coerce")
    pe_ret = pd.to_numeric(work.get("pe_forward_return"), errors="coerce")
    action = work["action"].astype(str)

    ce_trade_mask = (action == "BUY_CE") & ce_valid & ce_ret.notna()
    pe_trade_mask = (action == "BUY_PE") & pe_valid & pe_ret.notna()
    net_returns = np.concatenate(
        [
            (ce_ret.loc[ce_trade_mask].to_numpy(dtype=float) - float(cost_per_trade)),
            (pe_ret.loc[pe_trade_mask].to_numpy(dtype=float) - float(cost_per_trade)),
        ]
    )
    rows_total = int(len(work))
    trades_total = int(len(net_returns))
    days = int(_as_day_series(work).dropna().nunique())

    win_rate = float(np.mean(net_returns > 0.0)) if trades_total > 0 else 0.0
    mean_net = float(np.mean(net_returns)) if trades_total > 0 else 0.0
    net_sum = float(np.sum(net_returns)) if trades_total > 0 else 0.0
    return {
        "rows_total": rows_total,
        "days": days,
        "trades": trades_total,
        "avg_trades_per_day": float(trades_total / max(1, days)),
        "trade_rate": float(trades_total / max(1, rows_total)),
        "ce_trades": int(np.sum(ce_trade_mask.to_numpy())),
        "pe_trades": int(np.sum(pe_trade_mask.to_numpy())),
        "win_rate": win_rate,
        "mean_net_return_per_trade": mean_net,
        "net_return_sum": net_sum,
    }


def _latest_days_slice(frame: pd.DataFrame, latest_days: int) -> pd.DataFrame:
    if latest_days <= 0:
        return frame
    day_series = _as_day_series(frame)
    unique_days = sorted([d for d in day_series.dropna().unique().tolist() if d is not None])
    if len(unique_days) <= latest_days:
        return frame
    keep = set(unique_days[-int(latest_days) :])
    return frame.loc[day_series.isin(keep)].copy()


def _profile_id_from_threshold_path(threshold_path: Path) -> str:
    # .../config/profiles/{profile_id}/threshold_report.json
    parent = threshold_path.parent
    if parent.name and parent.name.lower() != "profiles":
        return str(parent.name)
    return "eval"


def _model_group_from_model_path(model_path: Path) -> str:
    # .../models/by_features/{group}/model/model.joblib -> {group}
    try:
        parts = [p.lower() for p in model_path.parts]
        idx = parts.index("by_features")
        group_parts = model_path.parts[idx + 1 : -2]
        if group_parts:
            return "/".join(group_parts)
    except Exception:
        pass
    try:
        return str(model_path.parent.parent.name)
    except Exception:
        return "model_group"


def run_evaluation(
    *,
    model_package_path: Path,
    threshold_report_path: Path,
    train_path: Path,
    valid_path: Path,
    eval_path: Path,
    profile_id: Optional[str],
    output_dir: Optional[Path],
    latest_days: int,
    oos_split: str,
    missing_policy: str,
) -> Dict[str, object]:
    model_package = load_model_package(model_package_path)
    thresholds = load_thresholds(threshold_report_path)

    splits: Dict[str, pd.DataFrame] = {
        "train": _load_frame(train_path),
        "valid": _load_frame(valid_path),
        "eval": _load_frame(eval_path),
    }
    if oos_split not in splits:
        raise ValueError(f"invalid --oos-split: {oos_split}")

    split_reports: Dict[str, Dict[str, object]] = {}
    for split_name, split_frame in splits.items():
        probs, validation = predict_probabilities_from_frame(
            split_frame,
            model_package,
            missing_policy_override=missing_policy,
            context=f"model_eval:{split_name}",
        )
        ce_side = _evaluate_side(
            frame=split_frame,
            prob=probs["ce_prob"].to_numpy(dtype=float),
            side="ce",
            threshold=float(thresholds.ce),
            cost_per_trade=float(thresholds.cost_per_trade),
        )
        pe_side = _evaluate_side(
            frame=split_frame,
            prob=probs["pe_prob"].to_numpy(dtype=float),
            side="pe",
            threshold=float(thresholds.pe),
            cost_per_trade=float(thresholds.cost_per_trade),
        )
        dual = _compute_dual_summary(
            frame=split_frame,
            probs=probs,
            ce_threshold=float(thresholds.ce),
            pe_threshold=float(thresholds.pe),
            cost_per_trade=float(thresholds.cost_per_trade),
        )
        split_reports[split_name] = {
            "rows": int(len(split_frame)),
            "input_contract": validation,
            "ce": ce_side,
            "pe": pe_side,
            "dual": dual,
        }

    oos_frame = splits[oos_split]
    oos_probs, _ = predict_probabilities_from_frame(
        oos_frame,
        model_package,
        missing_policy_override=missing_policy,
        context=f"model_eval:oos:{oos_split}",
    )
    full_oos = _compute_dual_summary(
        frame=oos_frame,
        probs=oos_probs,
        ce_threshold=float(thresholds.ce),
        pe_threshold=float(thresholds.pe),
        cost_per_trade=float(thresholds.cost_per_trade),
    )

    latest_frame = _latest_days_slice(oos_frame, latest_days=int(latest_days))
    latest_probs, _ = predict_probabilities_from_frame(
        latest_frame,
        model_package,
        missing_policy_override=missing_policy,
        context=f"model_eval:latest_oos:{oos_split}",
    )
    latest_oos = _compute_dual_summary(
        frame=latest_frame,
        probs=latest_probs,
        ce_threshold=float(thresholds.ce),
        pe_threshold=float(thresholds.pe),
        cost_per_trade=float(thresholds.cost_per_trade),
    )

    inferred_profile_id = str(profile_id or _profile_id_from_threshold_path(threshold_report_path)).strip()
    group_root = model_package_path.parent.parent
    if output_dir is None:
        output_dir = group_root / "reports" / "evaluation"
    output_dir.mkdir(parents=True, exist_ok=True)

    detail_report_path = output_dir / f"{inferred_profile_id}_evaluation_report.json"
    summary_report_path = output_dir / f"{inferred_profile_id}_eval_summary.json"

    detailed = {
        "created_at_utc": _utc_now(),
        "model_group": _model_group_from_model_path(model_package_path),
        "profile_id": inferred_profile_id,
        "model_package_path": str(model_package_path).replace("\\", "/"),
        "threshold_report_path": str(threshold_report_path).replace("\\", "/"),
        "feature_profile": model_package.get("feature_profile"),
        "trained_side": model_package.get("trained_side"),
        "thresholds": {
            "ce_threshold": float(thresholds.ce),
            "pe_threshold": float(thresholds.pe),
            "cost_per_trade": float(thresholds.cost_per_trade),
        },
        "config": {
            "oos_split": oos_split,
            "latest_days": int(latest_days),
            "missing_policy": str(missing_policy),
        },
        "splits": split_reports,
        "full_oos": full_oos,
        "latest_oos_slice": latest_oos,
    }
    summary = {
        "created_at_utc": _utc_now(),
        "model_group": detailed["model_group"],
        "profile_id": inferred_profile_id,
        "config": detailed["config"],
        "thresholds": detailed["thresholds"],
        "full_oos": full_oos,
        "latest_oos_slice": latest_oos,
    }

    detail_report_path.write_text(json.dumps(detailed, indent=2), encoding="utf-8")
    summary_report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return {
        "report": detailed,
        "outputs": {
            "evaluation_report_json": str(detail_report_path).replace("\\", "/"),
            "eval_summary_json": str(summary_report_path).replace("\\", "/"),
        },
    }


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate trained CE/PE model package on train/valid/eval splits.")
    parser.add_argument("--model-package", required=True, help="Path to model joblib")
    parser.add_argument("--threshold-report", required=True, help="Path to threshold_report.json")
    parser.add_argument("--train", required=True, help="Train parquet")
    parser.add_argument("--valid", required=True, help="Valid parquet")
    parser.add_argument("--eval", required=True, help="Eval parquet")
    parser.add_argument("--profile-id", default=None, help="Profile id for output filenames (default: infer from threshold path)")
    parser.add_argument("--output-dir", default=None, help="Evaluation output directory (default: model_group/reports/evaluation)")
    parser.add_argument("--latest-days", type=int, default=20, help="Latest N OOS days for latest_oos_slice")
    parser.add_argument("--oos-split", default="eval", choices=["train", "valid", "eval"])
    parser.add_argument("--missing-policy", default="error", choices=["error", "warn", "ignore"])
    args = parser.parse_args(list(argv) if argv is not None else None)

    out = run_evaluation(
        model_package_path=Path(args.model_package),
        threshold_report_path=Path(args.threshold_report),
        train_path=Path(args.train),
        valid_path=Path(args.valid),
        eval_path=Path(args.eval),
        profile_id=args.profile_id,
        output_dir=(Path(args.output_dir) if args.output_dir else None),
        latest_days=int(args.latest_days),
        oos_split=str(args.oos_split),
        missing_policy=str(args.missing_policy),
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
