from typing import Dict, Sequence

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score


def classification_metrics(y_true: np.ndarray, y_prob: np.ndarray) -> Dict[str, float]:
    y = np.asarray(y_true).astype(int)
    p = np.asarray(y_prob).astype(float)
    out: Dict[str, float] = {
        "brier": float(brier_score_loss(y, p)),
        "positive_rate": float(np.mean(y)) if len(y) else 0.0,
    }
    if len(np.unique(y)) >= 2:
        out["roc_auc"] = float(roc_auc_score(y, p))
        out["pr_auc"] = float(average_precision_score(y, p))
    else:
        out["roc_auc"] = float("nan")
        out["pr_auc"] = float("nan")
    return out


def max_drawdown_pct(returns: Sequence[float]) -> float:
    r = np.asarray(list(returns), dtype=float)
    if len(r) == 0:
        return 0.0
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    dd = (equity / np.where(peak == 0.0, 1.0, peak)) - 1.0
    return float(abs(np.nanmin(dd)))


def profit_factor(returns: Sequence[float]) -> float:
    r = np.asarray(list(returns), dtype=float)
    if len(r) == 0:
        return 0.0
    gp = float(np.sum(r[r > 0.0]))
    gl = float(abs(np.sum(r[r < 0.0])))
    if gl == 0.0:
        return float("inf") if gp > 0 else 0.0
    return float(gp / gl)


def threshold_stats(prob: np.ndarray, forward_ret: np.ndarray, thr: float, cost_per_trade: float) -> Dict[str, float]:
    p = np.asarray(prob, dtype=float)
    r = np.asarray(forward_ret, dtype=float)
    mask = p >= float(thr)
    trades = int(np.sum(mask))
    if trades == 0:
        return {
            "threshold": float(thr),
            "trades": 0,
            "trade_rate": 0.0,
            "mean_net_per_trade": 0.0,
            "total_net_return": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
        }
    net = r[mask] - float(cost_per_trade)
    return {
        "threshold": float(thr),
        "trades": trades,
        "trade_rate": float(trades / max(1, len(p))),
        "mean_net_per_trade": float(np.mean(net)),
        "total_net_return": float(np.sum(net)),
        "profit_factor": float(profit_factor(net)),
        "max_drawdown_pct": float(max_drawdown_pct(net)),
    }



def threshold_diagnostics(
    prob: np.ndarray,
    forward_ret: np.ndarray,
    cost_per_trade: float,
    grid_points: int = 10,
) -> str:
    """Human-readable table printed when threshold selection fails.
    Shows prob/return distributions and per-threshold stats so you can
    immediately see whether the problem is model calibration, bad labels,
    or cost drag overwhelming returns."""
    p = np.asarray(prob, dtype=float)
    r = np.asarray(forward_ret, dtype=float)
    lines = ["── Threshold Diagnostic ──────────────────────────────────────────"]
    lines.append(f"Samples              : {len(p)}")
    lines.append(f"Prob  [min,max,mean] : [{np.min(p):.4f}, {np.max(p):.4f}, {np.mean(p):.4f}]  std={np.std(p):.4f}")
    if len(r):
        lines.append(f"Return[min,max,mean] : [{np.min(r):.5f}, {np.max(r):.5f}, {np.mean(r):.5f}]")
        win_rate = float(np.mean(r > 0))
        win_after_cost = float(np.mean(r > cost_per_trade))
        lines.append(f"Win-rate (raw)       : {win_rate:.1%}   Win-rate (after cost {cost_per_trade}): {win_after_cost:.1%}")
        cost_drag = cost_per_trade / max(abs(float(np.mean(r))), 1e-9) * 100
        lines.append(f"Cost as %% of |mean|  : {cost_drag:.1f}%%  {'⚠ cost exceeds typical return' if cost_drag > 50 else 'ok'}")
    lines.append("")
    lines.append(f"{'Threshold':>9} | {'Trades':>7} | {'Trade%':>6} | {'PF':>6} | {'MDD':>6} | {'MeanNet':>9}")
    lines.append("-" * 60)
    thresholds = np.percentile(p, np.linspace(50, 99, grid_points))
    for thr in thresholds:
        mask = p >= thr
        n = int(np.sum(mask))
        if n == 0:
            lines.append(f"  {thr:.4f}  |       0 |    0%  |   n/a |    n/a |      n/a")
            continue
        net = r[mask] - cost_per_trade
        pf = profit_factor(net)
        mdd = max_drawdown_pct(net)
        mean_net = float(np.mean(net))
        lines.append(
            f"  {thr:.4f}  | {n:7d} | {n/len(p):5.1%} | {pf:6.2f} | {mdd:6.3f} | {mean_net:+.5f}"
        )
    lines.append("──────────────────────────────────────────────────────────────────")
    return "\n".join(lines)
