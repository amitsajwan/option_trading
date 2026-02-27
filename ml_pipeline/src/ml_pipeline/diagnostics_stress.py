import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone

from .train_baseline import compute_metrics
from .training_cycle import ConstantProbModel, QuantileClipper


IST = timezone(timedelta(hours=5, minutes=30))
LABEL_TARGET_BASE = "base_label"
LABEL_TARGET_PATH_TP_SL = "path_tp_sl"
LABEL_TARGET_CHOICES: Tuple[str, ...] = (LABEL_TARGET_BASE, LABEL_TARGET_PATH_TP_SL)
SELECTION_MODE_THRESHOLD = "threshold"
SELECTION_MODE_TOPK = "topk"
SELECTION_MODE_CHOICES: Tuple[str, ...] = (SELECTION_MODE_THRESHOLD, SELECTION_MODE_TOPK)

# Compatibility for model packages persisted when training_cycle was executed as __main__.
globals()["QuantileClipper"] = QuantileClipper
globals()["ConstantProbModel"] = ConstantProbModel


def _ensure_sorted(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    out["trade_date"] = out["trade_date"].astype(str)
    return out


def _split_chronological(df: pd.DataFrame, train_ratio: float, valid_ratio: float) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n = len(df)
    if n < 30:
        raise ValueError("not enough rows for diagnostics split")
    train_end = int(np.floor(n * float(train_ratio)))
    valid_end = int(np.floor(n * (float(train_ratio) + float(valid_ratio))))
    train_end = max(10, min(train_end, n - 10))
    valid_end = max(train_end + 5, min(valid_end, n - 5))
    return df.iloc[:train_end].copy(), df.iloc[train_end:valid_end].copy(), df.iloc[valid_end:].copy()


def _load_selection_policy(path: Path) -> Dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    # T31 format
    if isinstance(payload, dict) and "dual_mode_policy" in payload:
        policy = payload.get("dual_mode_policy", {})
        mode = str(policy.get("selection_mode", SELECTION_MODE_THRESHOLD)).lower()
        label_target = str(payload.get("label_target", LABEL_TARGET_BASE)).lower()
        if mode == SELECTION_MODE_TOPK:
            topk = policy.get("topk_per_day")
            if topk is None:
                raise ValueError("topk selection mode missing topk_per_day")
            return {
                "selection_mode": SELECTION_MODE_TOPK,
                "topk_per_day": int(topk),
                "ce_threshold": None,
                "pe_threshold": None,
                "label_target": label_target,
            }
        ce = policy.get("ce_threshold")
        pe = policy.get("pe_threshold")
        if ce is None or pe is None:
            raise ValueError("threshold report missing CE/PE thresholds")
        return {
            "selection_mode": SELECTION_MODE_THRESHOLD,
            "topk_per_day": None,
            "ce_threshold": float(ce),
            "pe_threshold": float(pe),
            "label_target": label_target,
        }
    # T08 format fallback
    ce = payload.get("ce", {}).get("selected_threshold")
    pe = payload.get("pe", {}).get("selected_threshold")
    if ce is None or pe is None:
        raise ValueError("threshold report missing CE/PE thresholds")
    return {
        "selection_mode": SELECTION_MODE_THRESHOLD,
        "topk_per_day": None,
        "ce_threshold": float(ce),
        "pe_threshold": float(pe),
        "label_target": LABEL_TARGET_BASE,
    }


def _load_thresholds(path: Path) -> Tuple[float, float]:
    policy = _load_selection_policy(path)
    ce = policy.get("ce_threshold")
    pe = policy.get("pe_threshold")
    if ce is None or pe is None:
        raise ValueError("threshold report missing CE/PE thresholds")
    return float(ce), float(pe)


def _side_frame(df: pd.DataFrame, side: str, label_target: str = LABEL_TARGET_BASE) -> pd.DataFrame:
    valid = f"{side}_label_valid"
    ret = f"{side}_forward_return"
    target_mode = str(label_target).lower()
    if target_mode == LABEL_TARGET_BASE:
        target = f"{side}_label"
        out = df[(df[valid] == 1.0) & df[target].notna() & df[ret].notna()].copy()
        out["target"] = out[target].astype(int)
    elif target_mode == LABEL_TARGET_PATH_TP_SL:
        exit_col = f"{side}_path_exit_reason"
        if exit_col not in df.columns:
            raise ValueError(f"missing required column for path_tp_sl target: {exit_col}")
        out = df[(df[valid] == 1.0) & df[ret].notna() & df[exit_col].notna()].copy()
        mapped = out[exit_col].astype(str).map({"tp": 1, "tp_sl_same_bar": 1, "sl": 0})
        out = out[mapped.notna()].copy()
        out["target"] = mapped.loc[out.index].astype(int)
    else:
        raise ValueError(f"unsupported label_target: {label_target}")
    return _ensure_sorted(out)


def _predict(model, x: pd.DataFrame) -> np.ndarray:
    return model.predict_proba(x)[:, 1]


def _learning_curve(
    model_template,
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    feature_columns: Sequence[str],
    target_col: str,
    fractions: Sequence[float],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    x_valid = valid_df.loc[:, list(feature_columns)]
    y_valid = valid_df[target_col].astype(int).to_numpy()
    n_train = len(train_df)
    for frac in fractions:
        k = max(10, int(np.floor(n_train * float(frac))))
        k = min(k, n_train)
        slice_train = train_df.iloc[:k].copy()
        x_train = slice_train.loc[:, list(feature_columns)]
        y_train = slice_train[target_col].astype(int).to_numpy()
        if len(np.unique(y_train)) < 2:
            p = float(np.mean(y_train)) if len(y_train) else 0.0
            train_prob = np.full(len(y_train), p, dtype=float)
            valid_prob = np.full(len(y_valid), p, dtype=float)
        else:
            model = clone(model_template)
            model.fit(x_train, y_train)
            train_prob = _predict(model, x_train)
            valid_prob = _predict(model, x_valid)
        train_metrics = compute_metrics(y_train, train_prob)
        valid_metrics = compute_metrics(y_valid, valid_prob)
        rows.append(
            {
                "fraction": float(frac),
                "train_rows": int(k),
                "train_brier": train_metrics["brier"],
                "valid_brier": valid_metrics["brier"],
                "train_f1": train_metrics["f1"],
                "valid_f1": valid_metrics["f1"],
            }
        )
    return rows


def _overfit_underfit_side(
    df: pd.DataFrame,
    side: str,
    model_template,
    feature_columns: Sequence[str],
    train_ratio: float,
    valid_ratio: float,
    label_target: str,
) -> Dict[str, object]:
    side_df = _side_frame(df, side=side, label_target=label_target)
    target_col = "target"
    train_df, valid_df, test_df = _split_chronological(side_df, train_ratio, valid_ratio)
    x_train = train_df.loc[:, list(feature_columns)]
    y_train = train_df[target_col].astype(int).to_numpy()
    x_valid = valid_df.loc[:, list(feature_columns)]
    y_valid = valid_df[target_col].astype(int).to_numpy()
    x_test = test_df.loc[:, list(feature_columns)]
    y_test = test_df[target_col].astype(int).to_numpy()

    if len(np.unique(y_train)) < 2:
        p = float(np.mean(y_train)) if len(y_train) else 0.0
        train_prob = np.full(len(y_train), p, dtype=float)
        valid_prob = np.full(len(y_valid), p, dtype=float)
        test_prob = np.full(len(y_test), p, dtype=float)
    else:
        model = clone(model_template)
        model.fit(x_train, y_train)
        train_prob = _predict(model, x_train)
        valid_prob = _predict(model, x_valid)
        test_prob = _predict(model, x_test)

    train_metrics = compute_metrics(y_train, train_prob)
    valid_metrics = compute_metrics(y_valid, valid_prob)
    test_metrics = compute_metrics(y_test, test_prob)
    brier_gap = float(valid_metrics["brier"] - train_metrics["brier"])
    f1_gap = float(train_metrics["f1"] - valid_metrics["f1"])
    learning = _learning_curve(
        model_template=model_template,
        train_df=train_df,
        valid_df=valid_df,
        feature_columns=feature_columns,
        target_col=target_col,
        fractions=[0.1, 0.2, 0.4, 0.6, 0.8, 1.0],
    )
    return {
        "rows": {"train": int(len(train_df)), "valid": int(len(valid_df)), "test": int(len(test_df))},
        "metrics": {"train": train_metrics, "valid": valid_metrics, "test": test_metrics},
        "gaps": {
            "brier_valid_minus_train": brier_gap,
            "f1_train_minus_valid": f1_gap,
        },
        "learning_curve": learning,
        "test_predictions": pd.DataFrame(
            {
                "timestamp": test_df["timestamp"].to_numpy(),
                "trade_date": test_df["trade_date"].to_numpy(),
                f"{side}_prob": test_prob,
                f"{side}_forward_return": test_df[f"{side}_forward_return"].to_numpy(dtype=float),
            }
        ),
    }


def _mode_metrics(
    merged: pd.DataFrame,
    mode: str,
    ce_threshold: Optional[float],
    pe_threshold: Optional[float],
    cost: float,
    slippage: float,
    selection_mode: str,
    topk_per_day: int,
) -> Dict[str, object]:
    rows = int(len(merged))
    ce_trades = 0
    pe_trades = 0
    nets: List[float] = []
    m = str(mode).lower()
    policy_mode = str(selection_mode).lower()
    if policy_mode == SELECTION_MODE_THRESHOLD:
        ce_thr = float(ce_threshold if ce_threshold is not None else 1.1)
        pe_thr = float(pe_threshold if pe_threshold is not None else 1.1)
        for row in merged.itertuples(index=False):
            ce_prob = float(getattr(row, "ce_prob"))
            pe_prob = float(getattr(row, "pe_prob"))
            side = None
            if m == "ce_only":
                if ce_prob >= ce_thr:
                    side = "CE"
            elif m == "pe_only":
                if pe_prob >= pe_thr:
                    side = "PE"
            else:
                if ce_prob >= ce_thr and pe_prob >= pe_thr:
                    side = "CE" if ce_prob >= pe_prob else "PE"
                elif ce_prob >= ce_thr:
                    side = "CE"
                elif pe_prob >= pe_thr:
                    side = "PE"
            if side is None:
                continue
            if side == "CE":
                ce_trades += 1
                gross = float(getattr(row, "ce_forward_return"))
            else:
                pe_trades += 1
                gross = float(getattr(row, "pe_forward_return"))
            nets.append(gross - float(cost) - float(slippage))
    elif policy_mode == SELECTION_MODE_TOPK:
        if len(merged) > 0:
            work = merged.copy()
            if m == "ce_only":
                work["side"] = "CE"
                work["score"] = work["ce_prob"].to_numpy(dtype=float)
                work["gross"] = work["ce_forward_return"].to_numpy(dtype=float)
            elif m == "pe_only":
                work["side"] = "PE"
                work["score"] = work["pe_prob"].to_numpy(dtype=float)
                work["gross"] = work["pe_forward_return"].to_numpy(dtype=float)
            else:
                ce_prob_arr = work["ce_prob"].to_numpy(dtype=float)
                pe_prob_arr = work["pe_prob"].to_numpy(dtype=float)
                ce_side = ce_prob_arr >= pe_prob_arr
                work["side"] = np.where(ce_side, "CE", "PE")
                work["score"] = np.where(ce_side, ce_prob_arr, pe_prob_arr)
                work["gross"] = np.where(ce_side, work["ce_forward_return"].to_numpy(dtype=float), work["pe_forward_return"].to_numpy(dtype=float))
            chosen = (
                work.sort_values(["trade_date", "score"], ascending=[True, False], kind="mergesort")
                .groupby("trade_date", sort=False)
                .head(max(1, int(topk_per_day)))
            )
            ce_trades = int((chosen["side"] == "CE").sum())
            pe_trades = int((chosen["side"] == "PE").sum())
            nets = (chosen["gross"].to_numpy(dtype=float) - float(cost) - float(slippage)).tolist()
    else:
        raise ValueError(f"unsupported selection_mode: {selection_mode}")
    trades = int(len(nets))
    net_sum = float(np.sum(nets)) if nets else 0.0
    return {
        "mode": m,
        "selection_mode": policy_mode,
        "topk_per_day": (int(topk_per_day) if policy_mode == SELECTION_MODE_TOPK else None),
        "rows_total": rows,
        "trades_total": trades,
        "trade_rate": (float(trades / rows) if rows > 0 else 0.0),
        "ce_trades": int(ce_trades),
        "pe_trades": int(pe_trades),
        "net_return_sum": net_sum,
        "mean_net_return_per_trade": (float(np.mean(nets)) if nets else 0.0),
        "win_rate": (float(np.mean(np.asarray(nets) > 0.0)) if nets else 0.0),
    }


def run_diagnostics_stress(
    labeled_df: pd.DataFrame,
    model_package: Dict[str, object],
    ce_threshold: Optional[float],
    pe_threshold: Optional[float],
    train_ratio: float = 0.70,
    valid_ratio: float = 0.15,
    cost_grid: Sequence[float] = (0.0006, 0.0010, 0.0015),
    slippage_grid: Sequence[float] = (0.0, 0.0005, 0.0010),
    selection_mode: str = SELECTION_MODE_THRESHOLD,
    topk_per_day: int = 10,
    label_target: str = LABEL_TARGET_BASE,
) -> Dict[str, object]:
    policy_mode = str(selection_mode).lower()
    if policy_mode not in SELECTION_MODE_CHOICES:
        raise ValueError(f"unsupported selection_mode: {selection_mode}")
    if int(topk_per_day) <= 0:
        raise ValueError("topk_per_day must be >= 1")
    target_mode = str(label_target).lower()
    if target_mode not in LABEL_TARGET_CHOICES:
        raise ValueError(f"unsupported label_target: {label_target}")
    frame = _ensure_sorted(labeled_df)
    feature_columns = list(model_package["feature_columns"])
    ce_model_template = model_package["models"]["ce"]
    pe_model_template = model_package["models"]["pe"]

    ce_diag = _overfit_underfit_side(
        df=frame,
        side="ce",
        model_template=ce_model_template,
        feature_columns=feature_columns,
        train_ratio=train_ratio,
        valid_ratio=valid_ratio,
        label_target=target_mode,
    )
    pe_diag = _overfit_underfit_side(
        df=frame,
        side="pe",
        model_template=pe_model_template,
        feature_columns=feature_columns,
        train_ratio=train_ratio,
        valid_ratio=valid_ratio,
        label_target=target_mode,
    )

    ce_test = ce_diag.pop("test_predictions")
    pe_test = pe_diag.pop("test_predictions")
    merged_test = ce_test.merge(pe_test, on=["timestamp", "trade_date"], how="inner")

    stress_rows: List[Dict[str, object]] = []
    for cost in cost_grid:
        for slip in slippage_grid:
            for mode in ("ce_only", "pe_only", "dual"):
                row = _mode_metrics(
                    merged=merged_test,
                    mode=mode,
                    ce_threshold=(float(ce_threshold) if ce_threshold is not None else None),
                    pe_threshold=(float(pe_threshold) if pe_threshold is not None else None),
                    cost=float(cost),
                    slippage=float(slip),
                    selection_mode=policy_mode,
                    topk_per_day=int(topk_per_day),
                )
                row["cost_per_trade"] = float(cost)
                row["slippage_per_trade"] = float(slip)
                stress_rows.append(row)

    return {
        "created_at_ist": datetime.now(IST).isoformat(),
        "task": "T32",
        "status": "completed",
        "rows_total": int(len(frame)),
        "days_total": int(frame["trade_date"].astype(str).nunique()) if "trade_date" in frame.columns else 0,
        "feature_count": int(len(feature_columns)),
        "feature_columns": feature_columns,
        "label_target": target_mode,
        "thresholds": {"ce": (float(ce_threshold) if ce_threshold is not None else None), "pe": (float(pe_threshold) if pe_threshold is not None else None)},
        "selection_policy": {
            "selection_mode": policy_mode,
            "topk_per_day": (int(topk_per_day) if policy_mode == SELECTION_MODE_TOPK else None),
            "ce_threshold": (float(ce_threshold) if ce_threshold is not None else None),
            "pe_threshold": (float(pe_threshold) if pe_threshold is not None else None),
        },
        "split_config": {"train_ratio": float(train_ratio), "valid_ratio": float(valid_ratio)},
        "overfit_underfit": {"ce": ce_diag, "pe": pe_diag},
        "cost_slippage_stress": stress_rows,
    }


def _summary_md(report: Dict[str, object]) -> str:
    ce = report["overfit_underfit"]["ce"]["gaps"]
    pe = report["overfit_underfit"]["pe"]["gaps"]
    selection_policy = report.get("selection_policy", {})
    # baseline scenario for quick read
    baseline = None
    for row in report["cost_slippage_stress"]:
        if row["mode"] == "dual" and abs(row["cost_per_trade"] - 0.0006) < 1e-12 and abs(row["slippage_per_trade"] - 0.0) < 1e-12:
            baseline = row
            break
    lines = [
        "# T32 Diagnostics + Stress Summary",
        "",
        f"- Created (IST): `{report['created_at_ist']}`",
        f"- Rows: `{report['rows_total']}` over `{report['days_total']}` days",
        f"- Features: `{report['feature_count']}`",
        f"- Label target: `{report.get('label_target', LABEL_TARGET_BASE)}`",
        f"- Selection mode: `{selection_policy.get('selection_mode', SELECTION_MODE_THRESHOLD)}`",
        "",
        "## Overfit/Underfit Gaps",
        f"- CE brier(valid-train): `{ce['brier_valid_minus_train']}`",
        f"- CE f1(train-valid): `{ce['f1_train_minus_valid']}`",
        f"- PE brier(valid-train): `{pe['brier_valid_minus_train']}`",
        f"- PE f1(train-valid): `{pe['f1_train_minus_valid']}`",
    ]
    if baseline is not None:
        lines.extend(
            [
                "",
                "## Baseline Stress Point (dual, cost=0.0006, slippage=0.0)",
                f"- Trades: `{baseline['trades_total']}`",
                f"- Trade rate: `{baseline['trade_rate']}`",
                f"- Net return sum: `{baseline['net_return_sum']}`",
                f"- Mean net/trade: `{baseline['mean_net_return_per_trade']}`",
                f"- Win rate: `{baseline['win_rate']}`",
            ]
        )
    return "\n".join(lines) + "\n"


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T32 overfit/underfit diagnostics + cost/slippage stress")
    parser.add_argument("--labeled-data", default="ml_pipeline/artifacts/t29_2y_auto_t05_labeled_features.parquet")
    parser.add_argument("--model-package", default="ml_pipeline/artifacts/t29_2y_auto_best_model.joblib")
    parser.add_argument("--threshold-report", default="ml_pipeline/artifacts/t31_calibration_threshold_report.json")
    parser.add_argument("--label-target", default=None, choices=list(LABEL_TARGET_CHOICES))
    parser.add_argument("--selection-mode", default=None, choices=list(SELECTION_MODE_CHOICES))
    parser.add_argument("--topk-per-day", type=int, default=None)
    parser.add_argument("--train-ratio", type=float, default=0.70)
    parser.add_argument("--valid-ratio", type=float, default=0.15)
    parser.add_argument("--cost-grid", default="0.0006,0.0010,0.0015")
    parser.add_argument("--slippage-grid", default="0.0,0.0005,0.0010")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t32_diagnostics_stress_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t32_diagnostics_stress_summary.md")
    args = parser.parse_args(list(argv) if argv is not None else None)

    data_path = Path(args.labeled_data)
    model_path = Path(args.model_package)
    threshold_path = Path(args.threshold_report)
    if not data_path.exists():
        print(f"ERROR: labeled dataset not found: {data_path}")
        return 2
    if not model_path.exists():
        print(f"ERROR: model package not found: {model_path}")
        return 2
    if not threshold_path.exists():
        print(f"ERROR: threshold report not found: {threshold_path}")
        return 2

    df = pd.read_parquet(data_path)
    model_package = joblib.load(model_path)
    if not isinstance(model_package, dict) or "models" not in model_package or "feature_columns" not in model_package:
        raise ValueError("invalid model package format")
    policy = _load_selection_policy(threshold_path)
    selection_mode = str(args.selection_mode) if args.selection_mode is not None else str(policy.get("selection_mode", SELECTION_MODE_THRESHOLD))
    label_target = str(args.label_target) if args.label_target is not None else str(policy.get("label_target", LABEL_TARGET_BASE))
    topk_per_day = int(args.topk_per_day) if args.topk_per_day is not None else int(policy.get("topk_per_day") or 10)
    ce_thr = policy.get("ce_threshold")
    pe_thr = policy.get("pe_threshold")
    if selection_mode == SELECTION_MODE_THRESHOLD and (ce_thr is None or pe_thr is None):
        raise ValueError("threshold mode requires CE/PE thresholds in threshold report")

    cost_grid = [float(x) for x in str(args.cost_grid).split(",") if str(x).strip()]
    slippage_grid = [float(x) for x in str(args.slippage_grid).split(",") if str(x).strip()]

    report = run_diagnostics_stress(
        labeled_df=df,
        model_package=model_package,
        ce_threshold=(float(ce_thr) if ce_thr is not None else None),
        pe_threshold=(float(pe_thr) if pe_thr is not None else None),
        train_ratio=float(args.train_ratio),
        valid_ratio=float(args.valid_ratio),
        cost_grid=cost_grid,
        slippage_grid=slippage_grid,
        selection_mode=selection_mode,
        topk_per_day=topk_per_day,
        label_target=label_target,
    )

    report_path = Path(args.report_out)
    summary_path = Path(args.summary_out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_path.write_text(_summary_md(report), encoding="utf-8")

    print(f"Rows: {report['rows_total']}")
    print(f"Stress points: {len(report['cost_slippage_stress'])}")
    print(f"Report: {report_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
