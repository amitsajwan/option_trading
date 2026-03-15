from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

from ..model_search.metrics import max_drawdown_pct, profit_factor


@dataclass(frozen=True)
class FuturesPromotionGates:
    long_roc_auc_min: float = 0.55
    short_roc_auc_min: float = 0.55
    brier_max: float = 0.22
    roc_auc_drift_max_abs: float = 0.05
    futures_pf_min: float = 1.5
    futures_max_drawdown_pct_max: float = 0.10
    futures_trades_min: int = 50
    side_share_min: float = 0.30
    side_share_max: float = 0.70
    block_rate_min: float = 0.25
    no_gate_relaxed_after_results: bool = True


def safe_float(value: object) -> Optional[float]:
    try:
        out = float(value)
    except Exception:
        return None
    return float(out) if np.isfinite(out) else None


def calibration_error(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10) -> Optional[float]:
    if len(y_true) == 0:
        return None
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_prob, dtype=float)
    order = np.argsort(p)
    y = y[order]
    p = p[order]
    bucket_edges = np.linspace(0, len(y), int(bins) + 1, dtype=int)
    total = float(len(y))
    ece = 0.0
    for idx in range(int(bins)):
        lo = int(bucket_edges[idx])
        hi = int(bucket_edges[idx + 1])
        if hi <= lo:
            continue
        gap = abs(float(np.mean(y[lo:hi])) - float(np.mean(p[lo:hi])))
        ece += ((hi - lo) / total) * gap
    return float(ece)


def safe_roc_auc(y_true: np.ndarray, y_prob: np.ndarray) -> Optional[float]:
    y = np.asarray(y_true, dtype=int)
    if len(np.unique(y)) < 2:
        return None
    return float(roc_auc_score(y, np.asarray(y_prob, dtype=float)))


def side_cols(frame: pd.DataFrame, side: str) -> tuple[str, str, str]:
    normalized = str(side).strip().lower()
    if normalized == "long":
        return ("long_label", "long_label_valid", "long_forward_return") if "long_label" in frame.columns else ("ce_label", "ce_label_valid", "ce_forward_return")
    if normalized == "short":
        return ("short_label", "short_label_valid", "short_forward_return") if "short_label" in frame.columns else ("pe_label", "pe_label_valid", "pe_forward_return")
    raise ValueError(f"unsupported side={side}")


def stage_a(frame: pd.DataFrame, probs: pd.DataFrame, gates: FuturesPromotionGates) -> Dict[str, object]:
    out: Dict[str, object] = {}
    passed = True
    for side, prob_col, roc_min in (("long", "ce_prob", float(gates.long_roc_auc_min)), ("short", "pe_prob", float(gates.short_roc_auc_min))):
        label_col, valid_col, _ = side_cols(frame, side)
        if label_col not in frame.columns or valid_col not in frame.columns:
            out[side] = {"available": False, "reason": f"missing {label_col}/{valid_col}"}
            passed = False
            continue
        valid = pd.to_numeric(frame[valid_col], errors="coerce").fillna(0.0) == 1.0
        labels = pd.to_numeric(frame.loc[valid, label_col], errors="coerce")
        score = pd.to_numeric(probs.loc[valid, prob_col], errors="coerce")
        usable = labels.notna() & score.notna()
        y = labels.loc[usable].to_numpy(dtype=float).astype(int)
        p = score.loc[usable].to_numpy(dtype=float)
        if len(y) < 10:
            out[side] = {"available": False, "reason": "insufficient rows"}
            passed = False
            continue
        roc = safe_roc_auc(y, p)
        brier = float(brier_score_loss(y, p))
        split = len(y) // 2
        roc_first = safe_roc_auc(y[:split], p[:split]) if split >= 10 else None
        roc_second = safe_roc_auc(y[split:], p[split:]) if (len(y) - split) >= 10 else None
        drift = float(roc_first - roc_second) if roc_first is not None and roc_second is not None else None
        gate_pass = bool((roc is not None and roc >= roc_min) and brier <= float(gates.brier_max) and (drift is not None and abs(drift) <= float(gates.roc_auc_drift_max_abs)))
        passed &= gate_pass
        out[side] = {"available": True, "rows": int(len(y)), "roc_auc": roc, "brier": brier, "calibration_error": calibration_error(y, p), "roc_auc_first_half": roc_first, "roc_auc_second_half": roc_second, "roc_auc_drift_half_split": drift, "gate_pass": gate_pass}
    return {"passed": bool(passed), "gates": {"long_roc_auc_min": float(gates.long_roc_auc_min), "short_roc_auc_min": float(gates.short_roc_auc_min), "brier_max": float(gates.brier_max), "roc_auc_drift_max_abs": float(gates.roc_auc_drift_max_abs)}, "sides": out}


def build_actions(ce_prob: np.ndarray, pe_prob: np.ndarray, ce_thr: float, pe_thr: float) -> np.ndarray:
    ce_ok = np.asarray(ce_prob, dtype=float) >= float(ce_thr)
    pe_ok = np.asarray(pe_prob, dtype=float) >= float(pe_thr)
    actions = np.full(len(ce_ok), "HOLD", dtype=object)
    actions[ce_ok & (~pe_ok)] = "BUY_CE"
    actions[pe_ok & (~ce_ok)] = "BUY_PE"
    both = ce_ok & pe_ok
    actions[both] = np.where(np.asarray(ce_prob, dtype=float)[both] >= np.asarray(pe_prob, dtype=float)[both], "BUY_CE", "BUY_PE")
    return actions


def _path_exit_reason(frame: pd.DataFrame, *, prefix: str, idx: int) -> str:
    col = f"{prefix}_path_exit_reason"
    if col not in frame.columns:
        return ""
    return str(frame.iloc[idx][col]).strip().lower()


def _path_gross_return(frame: pd.DataFrame, *, prefix: str, idx: int, fallback_return: float) -> Optional[float]:
    realized_col = f"{prefix}_realized_return"
    if realized_col in frame.columns:
        realized = safe_float(frame.iloc[idx][realized_col])
        if realized is not None:
            return float(realized)
    reason = _path_exit_reason(frame, prefix=prefix, idx=idx)
    if reason in {"tp", "tp_sl_same_bar"}:
        upper = safe_float(frame.iloc[idx][f"{prefix}_barrier_upper_return"]) if f"{prefix}_barrier_upper_return" in frame.columns else None
        if upper is not None:
            return float(upper)
    if reason == "sl":
        lower = safe_float(frame.iloc[idx][f"{prefix}_barrier_lower_return"]) if f"{prefix}_barrier_lower_return" in frame.columns else None
        if lower is not None:
            return float(-lower)
    fallback = safe_float(fallback_return)
    return float(fallback) if fallback is not None else None


def _trade_detail(frame: pd.DataFrame, *, idx: int, action: str, fallback_return: float, cost_per_trade: float) -> Optional[Dict[str, Any]]:
    prefix = "ce" if action == "BUY_CE" else "pe"
    gross_return = _path_gross_return(frame, prefix=prefix, idx=idx, fallback_return=fallback_return)
    if gross_return is None:
        return None
    exit_reason = _path_exit_reason(frame, prefix=prefix, idx=idx) or "forward"
    net_return = float(gross_return - float(cost_per_trade))
    return {
        "prefix": prefix,
        "exit_reason": exit_reason,
        "gross_return": float(gross_return),
        "net_return": net_return,
    }


def stage_b(frame: pd.DataFrame, probs: pd.DataFrame, ce_threshold: float, pe_threshold: float, cost_per_trade: float, gates: FuturesPromotionGates) -> Dict[str, object]:
    _, _, long_ret_col = side_cols(frame, "long")
    _, _, short_ret_col = side_cols(frame, "short")
    if long_ret_col not in frame.columns or short_ret_col not in frame.columns:
        return {"passed": False, "reason": f"missing return columns: {long_ret_col}/{short_ret_col}", "gates": {}}
    action = build_actions(pd.to_numeric(probs["ce_prob"], errors="coerce").to_numpy(dtype=float), pd.to_numeric(probs["pe_prob"], errors="coerce").to_numpy(dtype=float), float(ce_threshold), float(pe_threshold))
    long_ret = pd.to_numeric(frame[long_ret_col], errors="coerce").to_numpy(dtype=float)
    short_ret = pd.to_numeric(frame[short_ret_col], errors="coerce").to_numpy(dtype=float)
    net_returns: list[float] = []
    gross_returns: list[float] = []
    long_trades = 0
    short_trades = 0
    hold_count = 0
    invalid_trades = 0
    tp_trades = 0
    sl_trades = 0
    time_stop_trades = 0
    tp_sl_same_bar_trades = 0
    time_stop_gross_wins = 0
    time_stop_gross_losses = 0
    time_stop_gross_flats = 0
    time_stop_net_wins = 0
    time_stop_net_losses = 0
    time_stop_net_flats = 0
    for idx, act in enumerate(action):
        if act not in {"BUY_CE", "BUY_PE"}:
            hold_count += 1
            continue
        detail = _trade_detail(
            frame,
            idx=idx,
            action=str(act),
            fallback_return=(long_ret[idx] if act == "BUY_CE" else short_ret[idx]),
            cost_per_trade=float(cost_per_trade),
        )
        if detail is None:
            invalid_trades += 1
            hold_count += 1
            continue
        if act == "BUY_CE":
            long_trades += 1
        else:
            short_trades += 1
        gross_returns.append(float(detail["gross_return"]))
        net_returns.append(float(detail["net_return"]))
        exit_reason = str(detail["exit_reason"])
        if exit_reason == "tp":
            tp_trades += 1
        elif exit_reason == "tp_sl_same_bar":
            tp_trades += 1
            tp_sl_same_bar_trades += 1
        elif exit_reason == "sl":
            sl_trades += 1
        elif exit_reason == "time_stop":
            time_stop_trades += 1
            gross_return = float(detail["gross_return"])
            net_return = float(detail["net_return"])
            if gross_return > 0.0:
                time_stop_gross_wins += 1
            elif gross_return < 0.0:
                time_stop_gross_losses += 1
            else:
                time_stop_gross_flats += 1
            if net_return > 0.0:
                time_stop_net_wins += 1
            elif net_return < 0.0:
                time_stop_net_losses += 1
            else:
                time_stop_net_flats += 1
    trades = int(long_trades + short_trades)
    rows_total = int(len(frame))
    long_share = float(long_trades / max(1, trades)) if trades > 0 else 0.0
    short_share = float(short_trades / max(1, trades)) if trades > 0 else 0.0
    block_rate = float(hold_count / max(1, rows_total))
    pf = float(profit_factor(net_returns))
    mdd = float(max_drawdown_pct(net_returns))
    passed = bool(trades >= int(gates.futures_trades_min) and pf >= float(gates.futures_pf_min) and mdd <= float(gates.futures_max_drawdown_pct_max) and float(gates.side_share_min) <= long_share <= float(gates.side_share_max) and float(gates.side_share_min) <= short_share <= float(gates.side_share_max) and block_rate >= float(gates.block_rate_min))
    return {
        "passed": passed,
        "status": "computed",
        "rows_total": rows_total,
        "trades": trades,
        "long_trades": int(long_trades),
        "short_trades": int(short_trades),
        "hold_count": int(hold_count),
        "block_rate": block_rate,
        "long_share": long_share,
        "short_share": short_share,
        "profit_factor": pf,
        "gross_profit_factor": float(profit_factor(gross_returns)),
        "max_drawdown_pct": mdd,
        "win_rate": float(np.mean(np.asarray(net_returns, dtype=float) > 0.0)) if trades else 0.0,
        "gross_win_rate": float(np.mean(np.asarray(gross_returns, dtype=float) > 0.0)) if trades else 0.0,
        "mean_net_return_per_trade": float(np.mean(net_returns)) if trades else 0.0,
        "mean_gross_return_per_trade": float(np.mean(gross_returns)) if trades else 0.0,
        "net_return_sum": float(np.sum(net_returns)) if trades else 0.0,
        "gross_return_sum": float(np.sum(gross_returns)) if trades else 0.0,
        "tp_trades": int(tp_trades),
        "sl_trades": int(sl_trades),
        "time_stop_trades": int(time_stop_trades),
        "tp_sl_same_bar_trades": int(tp_sl_same_bar_trades),
        "invalid_trades": int(invalid_trades),
        "time_stop_gross_wins": int(time_stop_gross_wins),
        "time_stop_gross_losses": int(time_stop_gross_losses),
        "time_stop_gross_flats": int(time_stop_gross_flats),
        "time_stop_net_wins": int(time_stop_net_wins),
        "time_stop_net_losses": int(time_stop_net_losses),
        "time_stop_net_flats": int(time_stop_net_flats),
        "gates": {
            "futures_pf_min": float(gates.futures_pf_min),
            "futures_max_drawdown_pct_max": float(gates.futures_max_drawdown_pct_max),
            "futures_trades_min": int(gates.futures_trades_min),
            "side_share_min": float(gates.side_share_min),
            "side_share_max": float(gates.side_share_max),
            "block_rate_min": float(gates.block_rate_min),
        },
    }


def stage_c(frame: pd.DataFrame, stage_b_report: Dict[str, object]) -> Dict[str, object]:
    uses_alias_returns = bool(("long_forward_return" in frame.columns) and ("ce_forward_return" in frame.columns) and np.allclose(pd.to_numeric(frame["long_forward_return"], errors="coerce").to_numpy(dtype=float), pd.to_numeric(frame["ce_forward_return"], errors="coerce").to_numpy(dtype=float), equal_nan=True))
    return {"passed": True, "non_blocking": True, "uses_alias_returns": uses_alias_returns, "diagnostic_note": "CE/PE mapping diagnostics computed; for futures-labeled datasets CE/PE may alias LONG/SHORT.", "mapped_execution_proxy": {"trades": int(stage_b_report.get("trades", 0)), "net_return_sum": float(stage_b_report.get("net_return_sum", 0.0)), "profit_factor": float(stage_b_report.get("profit_factor", 0.0))}}


def positive_rate(frame: pd.DataFrame, *, label_col: str, valid_col: str) -> Optional[float]:
    if label_col not in frame.columns or valid_col not in frame.columns:
        return None
    valid = pd.to_numeric(frame[valid_col], errors="coerce").fillna(0.0) == 1.0
    y = pd.to_numeric(frame.loc[valid, label_col], errors="coerce").dropna()
    return float(y.mean()) if len(y) else None


def positive_rate_diagnostics(*, training_frame: Optional[pd.DataFrame], holdout_frame: pd.DataFrame, gap_flag_threshold: float = 0.08) -> Dict[str, object]:
    holdout_long_label, holdout_long_valid, _ = side_cols(holdout_frame, "long")
    holdout_short_label, holdout_short_valid, _ = side_cols(holdout_frame, "short")
    holdout_long = positive_rate(holdout_frame, label_col=holdout_long_label, valid_col=holdout_long_valid)
    holdout_short = positive_rate(holdout_frame, label_col=holdout_short_label, valid_col=holdout_short_valid)
    train_long = train_short = None
    if training_frame is not None:
        train_long_label, train_long_valid, _ = side_cols(training_frame, "long")
        train_short_label, train_short_valid, _ = side_cols(training_frame, "short")
        train_long = positive_rate(training_frame, label_col=train_long_label, valid_col=train_long_valid)
        train_short = positive_rate(training_frame, label_col=train_short_label, valid_col=train_short_valid)
    gap_long = float(abs(float(holdout_long) - float(train_long))) if holdout_long is not None and train_long is not None else None
    gap_short = float(abs(float(holdout_short) - float(train_short))) if holdout_short is not None and train_short is not None else None
    return {"training_positive_rate_long": train_long, "training_positive_rate_short": train_short, "holdout_positive_rate_long": holdout_long, "holdout_positive_rate_short": holdout_short, "holdout_train_pos_rate_gap_long": gap_long, "holdout_train_pos_rate_gap_short": gap_short, "flagged": bool((gap_long is not None and gap_long > float(gap_flag_threshold)) or (gap_short is not None and gap_short > float(gap_flag_threshold)))}
