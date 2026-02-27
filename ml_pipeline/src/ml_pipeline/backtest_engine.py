import argparse
import json
from dataclasses import asdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import DecisionConfig, TrainConfig
from .fill_model import FillModelConfig, config_to_dict as fill_config_to_dict, estimate_slippage_return
from .train_baseline import build_baseline_pipeline, select_feature_columns
from .walk_forward import build_day_folds


class ConstantProbModel:
    def __init__(self, prob_one: float):
        self.prob_one = float(prob_one)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = len(x)
        p1 = np.full(n, self.prob_one, dtype=float)
        p0 = 1.0 - p1
        return np.column_stack([p0, p1])


def entry_exit_timestamps(decision_ts: pd.Timestamp, horizon_minutes: int) -> Tuple[pd.Timestamp, pd.Timestamp]:
    ts = pd.Timestamp(decision_ts)
    entry_ts = ts + pd.Timedelta(minutes=1)
    exit_ts = ts + pd.Timedelta(minutes=int(horizon_minutes))
    return entry_ts, exit_ts


def _rows_for_days(df: pd.DataFrame, day_list: Sequence[str]) -> pd.DataFrame:
    mask = df["trade_date"].astype(str).isin(set(str(x) for x in day_list))
    return df.loc[mask].sort_values("timestamp").copy()


def _prepare_side_data(df: pd.DataFrame, side: str) -> pd.DataFrame:
    target_col = f"{side}_label"
    valid_col = f"{side}_label_valid"
    out = df[(df[valid_col] == 1.0) & df[target_col].notna()].copy()
    out[target_col] = out[target_col].astype(int)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return out


def _max_drawdown(cum_returns: pd.Series) -> float:
    if len(cum_returns) == 0:
        return 0.0
    running_peak = cum_returns.cummax()
    drawdowns = cum_returns - running_peak
    return float(drawdowns.min())


def _load_thresholds(path: Path) -> Tuple[float, float, Dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    ce = payload.get("ce", {}).get("selected_threshold")
    pe = payload.get("pe", {}).get("selected_threshold")
    if ce is None or pe is None:
        raise ValueError("threshold report missing ce/pe selected_threshold")
    return float(ce), float(pe), payload


def _train_config_from_report(payload: Dict[str, object], fallback: TrainConfig) -> TrainConfig:
    raw = payload.get("train_config") or {}
    return TrainConfig(
        train_ratio=float(raw.get("train_ratio", fallback.train_ratio)),
        valid_ratio=float(raw.get("valid_ratio", fallback.valid_ratio)),
        random_state=int(raw.get("random_state", fallback.random_state)),
        max_depth=int(raw.get("max_depth", fallback.max_depth)),
        n_estimators=int(raw.get("n_estimators", fallback.n_estimators)),
        learning_rate=float(raw.get("learning_rate", fallback.learning_rate)),
    )


def _walk_forward_config_from_report(payload: Dict[str, object]) -> Dict[str, int]:
    wf = payload.get("walk_forward_config") or {}
    return {
        "train_days": int(wf.get("train_days", 3)),
        "valid_days": int(wf.get("valid_days", 1)),
        "test_days": int(wf.get("test_days", 1)),
        "step_days": int(wf.get("step_days", 1)),
    }


def _filter_backtest_rows(df: pd.DataFrame) -> pd.DataFrame:
    required = [
        "timestamp",
        "trade_date",
        "ce_forward_return",
        "pe_forward_return",
        "ce_entry_price",
        "ce_exit_price",
        "pe_entry_price",
        "pe_exit_price",
        "label_horizon_minutes",
        "ce_label_valid",
        "pe_label_valid",
    ]
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"])
    out = out[(out["ce_label_valid"] == 1.0) & (out["pe_label_valid"] == 1.0)]
    out = out.dropna(subset=required)
    out = out.sort_values("timestamp").reset_index(drop=True)
    return out


def _build_fold_models(
    train_df: pd.DataFrame,
    feature_columns: Sequence[str],
    config: TrainConfig,
) -> Tuple[object, object]:
    ce_train = _prepare_side_data(train_df, "ce")
    pe_train = _prepare_side_data(train_df, "pe")
    if len(ce_train) == 0 or len(pe_train) == 0:
        raise ValueError("empty side training data in fold")
    ce_classes = np.unique(ce_train["ce_label"].to_numpy())
    if len(ce_classes) < 2:
        ce_model = ConstantProbModel(float(ce_classes[0]) if len(ce_classes) == 1 else 0.0)
    else:
        ce_model = build_baseline_pipeline(config)
        ce_model.fit(ce_train.loc[:, list(feature_columns)], ce_train["ce_label"].to_numpy())

    pe_classes = np.unique(pe_train["pe_label"].to_numpy())
    if len(pe_classes) < 2:
        pe_model = ConstantProbModel(float(pe_classes[0]) if len(pe_classes) == 1 else 0.0)
    else:
        pe_model = build_baseline_pipeline(config)
        pe_model.fit(pe_train.loc[:, list(feature_columns)], pe_train["pe_label"].to_numpy())
    return ce_model, pe_model


def _trade_side(ce_prob: float, pe_prob: float, ce_thr: float, pe_thr: float) -> Optional[str]:
    ce_signal = ce_prob >= ce_thr
    pe_signal = pe_prob >= pe_thr
    if ce_signal and pe_signal:
        return "CE" if ce_prob >= pe_prob else "PE"
    if ce_signal:
        return "CE"
    if pe_signal:
        return "PE"
    return None


def _reason_from_path(path_reason: str, intrabar_tie_break: str) -> str:
    pr = str(path_reason or "").strip().lower()
    if pr == "tp":
        return "tp"
    if pr == "sl":
        return "sl"
    if pr == "tp_sl_same_bar":
        return "tp" if str(intrabar_tie_break).lower() == "tp" else "sl"
    if pr in {"trail", "forced_eod"}:
        return pr
    return "time"


def _safe_float(value: object) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _forced_eod_time(value: str) -> Optional[time]:
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError:
        return None


def _compute_trade_outcome(
    row: pd.Series,
    side: str,
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
    horizon_minutes: int,
    selected_prob: float,
    selected_threshold: float,
    ce_prob: float,
    pe_prob: float,
    cost_per_trade: float,
    slippage_per_trade: float,
    slippage_model_component: float,
    execution_mode: str,
    intrabar_tie_break: str,
    forced_eod_exit_time: str,
) -> Dict[str, object]:
    prefix = "ce" if side == "CE" else "pe"
    entry_price = _safe_float(row.get(f"{prefix}_entry_price"))
    exit_price_default = _safe_float(row.get(f"{prefix}_exit_price"))
    forward_return = _safe_float(row.get(f"{prefix}_forward_return"))
    tp_price = _safe_float(row.get(f"{prefix}_tp_price"))
    sl_price = _safe_float(row.get(f"{prefix}_sl_price"))
    path_reason_raw = str(row.get(f"{prefix}_path_exit_reason", "time_stop"))
    first_hit_offset = _safe_float(row.get(f"{prefix}_first_hit_offset_min"))

    exit_reason = "time"
    exit_price = exit_price_default
    realized_return = forward_return
    realized_exit_ts = exit_ts

    if execution_mode == "path_v2":
        exit_reason = _reason_from_path(path_reason_raw, intrabar_tie_break=intrabar_tie_break)
        if exit_reason == "tp" and np.isfinite(entry_price) and entry_price > 0 and np.isfinite(tp_price):
            exit_price = tp_price
            realized_return = (tp_price - entry_price) / entry_price
            if np.isfinite(first_hit_offset):
                realized_exit_ts = entry_ts + pd.Timedelta(minutes=int(first_hit_offset))
        elif exit_reason == "sl" and np.isfinite(entry_price) and entry_price > 0 and np.isfinite(sl_price):
            exit_price = sl_price
            realized_return = (sl_price - entry_price) / entry_price
            if np.isfinite(first_hit_offset):
                realized_exit_ts = entry_ts + pd.Timedelta(minutes=int(first_hit_offset))
        elif exit_reason == "trail":
            trail_price = _safe_float(row.get(f"{prefix}_trail_exit_price"))
            if np.isfinite(entry_price) and entry_price > 0 and np.isfinite(trail_price):
                exit_price = trail_price
                realized_return = (trail_price - entry_price) / entry_price
            trail_offset = _safe_float(row.get(f"{prefix}_trail_exit_offset_min"))
            if np.isfinite(trail_offset):
                realized_exit_ts = entry_ts + pd.Timedelta(minutes=int(trail_offset))
        elif exit_reason == "forced_eod":
            feod_price = _safe_float(row.get(f"{prefix}_forced_eod_exit_price"))
            if np.isfinite(entry_price) and entry_price > 0 and np.isfinite(feod_price):
                exit_price = feod_price
                realized_return = (feod_price - entry_price) / entry_price
            feod_offset = _safe_float(row.get(f"{prefix}_forced_eod_exit_offset_min"))
            if np.isfinite(feod_offset):
                realized_exit_ts = entry_ts + pd.Timedelta(minutes=int(feod_offset))
        else:
            # Explicitly tag forced EOD if configured time is earlier than horizon exit.
            cutoff = _forced_eod_time(forced_eod_exit_time)
            if cutoff is not None and realized_exit_ts.time() > cutoff:
                exit_reason = "forced_eod"
                realized_exit_ts = pd.Timestamp.combine(realized_exit_ts.date(), cutoff)

    gross_return = float(realized_return)
    slippage_total = float(slippage_per_trade) + float(slippage_model_component)
    net_return = gross_return - float(cost_per_trade) - slippage_total
    return {
        "side": side,
        "ce_prob": float(ce_prob),
        "pe_prob": float(pe_prob),
        "selected_prob": float(selected_prob),
        "selected_threshold": float(selected_threshold),
        "entry_timestamp": entry_ts,
        "exit_timestamp": realized_exit_ts,
        "horizon_minutes": int(horizon_minutes),
        "entry_price": float(entry_price),
        "exit_price": float(exit_price),
        "gross_return": gross_return,
        "cost_per_trade": float(cost_per_trade),
        "slippage_per_trade": float(slippage_per_trade),
        "slippage_model_component": float(slippage_model_component),
        "slippage_total": float(slippage_total),
        "net_return": net_return,
        "execution_mode": execution_mode,
        "intrabar_tie_break": str(intrabar_tie_break).lower(),
        "path_exit_reason": path_reason_raw,
        "exit_reason": exit_reason,
    }


def compute_trade_outcome_from_row(
    row: pd.Series,
    side: str,
    decision_ts: pd.Timestamp,
    horizon_minutes: int,
    selected_prob: float,
    selected_threshold: float,
    ce_prob: float,
    pe_prob: float,
    cost_per_trade: float,
    slippage_per_trade: float,
    slippage_model_component: float,
    execution_mode: str = "fixed_horizon",
    intrabar_tie_break: str = "sl",
    forced_eod_exit_time: str = "15:24",
) -> Dict[str, object]:
    entry_ts, exit_ts = entry_exit_timestamps(decision_ts, horizon_minutes=horizon_minutes)
    return _compute_trade_outcome(
        row=row,
        side=side,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        horizon_minutes=horizon_minutes,
        selected_prob=selected_prob,
        selected_threshold=selected_threshold,
        ce_prob=ce_prob,
        pe_prob=pe_prob,
        cost_per_trade=cost_per_trade,
        slippage_per_trade=slippage_per_trade,
        slippage_model_component=slippage_model_component,
        execution_mode=execution_mode,
        intrabar_tie_break=intrabar_tie_break,
        forced_eod_exit_time=forced_eod_exit_time,
    )


def run_backtest(
    labeled_df: pd.DataFrame,
    ce_threshold: float,
    pe_threshold: float,
    cost_per_trade: float,
    train_config: TrainConfig,
    train_days: int,
    valid_days: int,
    test_days: int,
    step_days: int,
    execution_mode: str = "fixed_horizon",
    intrabar_tie_break: str = "sl",
    slippage_per_trade: float = 0.0,
    forced_eod_exit_time: str = "15:24",
    fill_model_config: Optional[FillModelConfig] = None,
) -> Tuple[pd.DataFrame, Dict[str, object]]:
    if execution_mode not in {"fixed_horizon", "path_v2"}:
        raise ValueError("execution_mode must be one of: fixed_horizon, path_v2")
    if str(intrabar_tie_break).lower() not in {"sl", "tp"}:
        raise ValueError("intrabar_tie_break must be one of: sl, tp")
    fill_cfg = fill_model_config if fill_model_config is not None else FillModelConfig()

    frame = _filter_backtest_rows(labeled_df)
    feature_columns = select_feature_columns(frame)
    if not feature_columns:
        raise ValueError("no feature columns available for backtest")

    days = sorted(frame["trade_date"].astype(str).unique().tolist())
    folds = build_day_folds(days, train_days=train_days, valid_days=valid_days, test_days=test_days, step_days=step_days)
    trade_rows: List[Dict[str, object]] = []
    fold_summaries: List[Dict[str, object]] = []

    for fold_idx, fold in enumerate(folds, start=1):
        train_df = _rows_for_days(frame, fold["train_days"])
        test_df = _rows_for_days(frame, fold["test_days"])
        if len(train_df) == 0 or len(test_df) == 0:
            fold_summaries.append(
                {
                    "fold_index": fold_idx,
                    "fold_ok": False,
                    "days": fold,
                    "error": "empty train/test split",
                }
            )
            continue

        ce_model, pe_model = _build_fold_models(train_df, feature_columns=feature_columns, config=train_config)
        x_test = test_df.loc[:, list(feature_columns)]
        ce_prob = ce_model.predict_proba(x_test)[:, 1]
        pe_prob = pe_model.predict_proba(x_test)[:, 1]

        fold_trade_count = 0
        fold_net_sum = 0.0
        fold_gross_sum = 0.0
        for i, row in enumerate(test_df.itertuples(index=False)):
            row_dict = row._asdict()
            side = _trade_side(float(ce_prob[i]), float(pe_prob[i]), ce_threshold, pe_threshold)
            if side is None:
                continue
            decision_ts = pd.Timestamp(row_dict["timestamp"])
            horizon = int(row_dict["label_horizon_minutes"])
            entry_ts, exit_ts = entry_exit_timestamps(decision_ts, horizon_minutes=horizon)
            if side == "CE":
                selected_prob = float(ce_prob[i])
                threshold = float(ce_threshold)
            else:
                selected_prob = float(pe_prob[i])
                threshold = float(pe_threshold)
            model_slippage = estimate_slippage_return(pd.Series(row_dict), side=side, config=fill_cfg)

            outcome = _compute_trade_outcome(
                row=pd.Series(row_dict),
                side=side,
                entry_ts=entry_ts,
                exit_ts=exit_ts,
                horizon_minutes=horizon,
                selected_prob=selected_prob,
                selected_threshold=threshold,
                ce_prob=float(ce_prob[i]),
                pe_prob=float(pe_prob[i]),
                cost_per_trade=float(cost_per_trade),
                slippage_per_trade=float(slippage_per_trade),
                slippage_model_component=float(model_slippage),
                execution_mode=execution_mode,
                intrabar_tie_break=intrabar_tie_break,
                forced_eod_exit_time=forced_eod_exit_time,
            )

            trade_rows.append(
                {
                    "fold_index": fold_idx,
                    "decision_timestamp": decision_ts,
                    "trade_date": str(row_dict["trade_date"]),
                    "ce_threshold": float(ce_threshold),
                    "pe_threshold": float(pe_threshold),
                    **outcome,
                }
            )
            fold_trade_count += 1
            fold_net_sum += float(outcome["net_return"])
            fold_gross_sum += float(outcome["gross_return"])

        fold_summaries.append(
            {
                "fold_index": fold_idx,
                "fold_ok": True,
                "days": fold,
                "test_rows": int(len(test_df)),
                "trades": int(fold_trade_count),
                "trade_rate": float(fold_trade_count / len(test_df)) if len(test_df) else 0.0,
                "gross_return_sum": float(fold_gross_sum),
                "net_return_sum": float(fold_net_sum),
            }
        )

    trades = pd.DataFrame(trade_rows)
    if len(trades) > 0:
        trades = trades.sort_values("decision_timestamp").reset_index(drop=True)
        trades["cum_net_return"] = trades["net_return"].cumsum()
    else:
        trades["cum_net_return"] = pd.Series(dtype=float)

    total_rows = int(sum(f.get("test_rows", 0) for f in fold_summaries if f.get("fold_ok")))
    total_trades = int(len(trades))
    ce_trades = int((trades["side"] == "CE").sum()) if total_trades else 0
    pe_trades = int((trades["side"] == "PE").sum()) if total_trades else 0
    gross_sum = float(trades["gross_return"].sum()) if total_trades else 0.0
    net_sum = float(trades["net_return"].sum()) if total_trades else 0.0
    mean_net = float(trades["net_return"].mean()) if total_trades else 0.0
    win_rate = float((trades["net_return"] > 0).mean()) if total_trades else 0.0
    max_dd = _max_drawdown(trades["cum_net_return"]) if total_trades else 0.0
    exit_reason_counts = (
        {str(k): int(v) for k, v in trades["exit_reason"].value_counts().to_dict().items()}
        if total_trades and "exit_reason" in trades.columns
        else {}
    )

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows_total": int(len(frame)),
        "feature_count": int(len(feature_columns)),
        "thresholds": {
            "ce": float(ce_threshold),
            "pe": float(pe_threshold),
        },
        "cost_per_trade": float(cost_per_trade),
        "slippage_per_trade": float(slippage_per_trade),
        "fill_model": fill_config_to_dict(fill_cfg),
        "execution_mode": execution_mode,
        "intrabar_tie_break": str(intrabar_tie_break).lower(),
        "forced_eod_exit_time": str(forced_eod_exit_time),
        "walk_forward_config": {
            "train_days": int(train_days),
            "valid_days": int(valid_days),
            "test_days": int(test_days),
            "step_days": int(step_days),
        },
        "fold_count": int(len(folds)),
        "fold_ok_count": int(sum(1 for f in fold_summaries if f.get("fold_ok"))),
        "trades_total": total_trades,
        "test_rows_total": total_rows,
        "trade_rate": float(total_trades / total_rows) if total_rows else 0.0,
        "ce_trades": ce_trades,
        "pe_trades": pe_trades,
        "gross_return_sum": gross_sum,
        "net_return_sum": net_sum,
        "mean_net_return_per_trade": mean_net,
        "win_rate": win_rate,
        "max_drawdown": max_dd,
        "mean_slippage_model_component": (
            float(trades["slippage_model_component"].mean()) if total_trades and "slippage_model_component" in trades.columns else 0.0
        ),
        "mean_slippage_total": (
            float(trades["slippage_total"].mean()) if total_trades and "slippage_total" in trades.columns else 0.0
        ),
        "exit_reason_counts": exit_reason_counts,
        "folds": fold_summaries,
    }
    return trades, report


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run fold-safe event-level backtest")
    parser.add_argument(
        "--labeled-data",
        default="ml_pipeline/artifacts/t05_labeled_features.parquet",
        help="Labeled data parquet",
    )
    parser.add_argument(
        "--threshold-report",
        default="ml_pipeline/artifacts/t08_threshold_report.json",
        help="Threshold optimization report",
    )
    parser.add_argument(
        "--trades-out",
        default="ml_pipeline/artifacts/t09_backtest_trades.parquet",
        help="Trade-level output parquet",
    )
    parser.add_argument(
        "--report-out",
        default="ml_pipeline/artifacts/t09_backtest_report.json",
        help="Backtest summary JSON output",
    )
    parser.add_argument("--ce-threshold", type=float, default=None)
    parser.add_argument("--pe-threshold", type=float, default=None)
    parser.add_argument("--cost-per-trade", type=float, default=None)
    parser.add_argument("--slippage-per-trade", type=float, default=0.0)
    parser.add_argument("--fill-model", default="constant", choices=["constant", "spread_fraction", "liquidity_adjusted"])
    parser.add_argument("--fill-constant", type=float, default=0.0)
    parser.add_argument("--fill-spread-fraction", type=float, default=0.5)
    parser.add_argument("--fill-volume-impact", type=float, default=0.02)
    parser.add_argument("--fill-min", type=float, default=0.0)
    parser.add_argument("--fill-max", type=float, default=0.01)
    parser.add_argument("--train-days", type=int, default=None)
    parser.add_argument("--valid-days", type=int, default=None)
    parser.add_argument("--test-days", type=int, default=None)
    parser.add_argument("--step-days", type=int, default=None)
    parser.add_argument(
        "--execution-mode",
        default="fixed_horizon",
        choices=["fixed_horizon", "path_v2"],
        help="Execution semantics. fixed_horizon uses t+H close return, path_v2 uses TP/SL/time path columns.",
    )
    parser.add_argument(
        "--intrabar-tie-break",
        default="sl",
        choices=["sl", "tp"],
        help="If both TP and SL hit in same bar, resolve to this side in path_v2 mode.",
    )
    parser.add_argument("--forced-eod-exit-time", default="15:24", help="HH:MM for path_v2 forced EOD tagging")
    args = parser.parse_args(list(argv) if argv is not None else None)

    labeled_path = Path(args.labeled_data)
    threshold_path = Path(args.threshold_report)
    if not labeled_path.exists():
        print(f"ERROR: labeled dataset not found: {labeled_path}")
        return 2
    if not threshold_path.exists():
        print(f"ERROR: threshold report not found: {threshold_path}")
        return 2

    ce_thr_report, pe_thr_report, threshold_payload = _load_thresholds(threshold_path)
    ce_threshold = float(args.ce_threshold) if args.ce_threshold is not None else ce_thr_report
    pe_threshold = float(args.pe_threshold) if args.pe_threshold is not None else pe_thr_report

    default_train = TrainConfig()
    train_cfg = _train_config_from_report(threshold_payload, fallback=default_train)
    wf_cfg = _walk_forward_config_from_report(threshold_payload)
    train_days = int(args.train_days) if args.train_days is not None else wf_cfg["train_days"]
    valid_days = int(args.valid_days) if args.valid_days is not None else wf_cfg["valid_days"]
    test_days = int(args.test_days) if args.test_days is not None else wf_cfg["test_days"]
    step_days = int(args.step_days) if args.step_days is not None else wf_cfg["step_days"]

    default_decision = DecisionConfig()
    report_decision = threshold_payload.get("decision_config") or {}
    cost_default = float(report_decision.get("cost_per_trade", default_decision.cost_per_trade))
    cost_per_trade = float(args.cost_per_trade) if args.cost_per_trade is not None else cost_default

    labeled = pd.read_parquet(labeled_path)
    fill_cfg = FillModelConfig(
        model=str(args.fill_model),
        constant_slippage=float(args.fill_constant),
        spread_fraction=float(args.fill_spread_fraction),
        volume_impact_coeff=float(args.fill_volume_impact),
        min_slippage=float(args.fill_min),
        max_slippage=float(args.fill_max),
    )
    trades, report = run_backtest(
        labeled_df=labeled,
        ce_threshold=ce_threshold,
        pe_threshold=pe_threshold,
        cost_per_trade=cost_per_trade,
        train_config=train_cfg,
        train_days=train_days,
        valid_days=valid_days,
        test_days=test_days,
        step_days=step_days,
        execution_mode=args.execution_mode,
        intrabar_tie_break=args.intrabar_tie_break,
        slippage_per_trade=float(args.slippage_per_trade),
        forced_eod_exit_time=args.forced_eod_exit_time,
        fill_model_config=fill_cfg,
    )

    trades_out = Path(args.trades_out)
    report_out = Path(args.report_out)
    trades_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(trades_out, index=False)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Input rows: {len(labeled)}")
    print(f"Trades: {report['trades_total']}")
    print(f"Trade rate: {report['trade_rate']}")
    print(f"Net return sum: {report['net_return_sum']}")
    print(f"Trades output: {trades_out}")
    print(f"Report output: {report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
