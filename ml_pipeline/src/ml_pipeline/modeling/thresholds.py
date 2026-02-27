from typing import Dict, List, Optional

import numpy as np

from .metrics import threshold_diagnostics, threshold_stats


RANKING_MODE_LEGACY = "legacy"
RANKING_MODE_BREAKOUT_PF_LOGTRADES = "breakout_pf_logtrades"


def _ranking_score_breakout(row: Dict[str, float]) -> float:
    pf_raw = float(row.get("profit_factor", 0.0))
    pf = 10.0 if not np.isfinite(pf_raw) else max(0.0, pf_raw)
    trades = max(0.0, float(row.get("trades", 0.0)))
    return float(pf * np.log1p(trades))


def _single_sort_key(row: Dict[str, float], ranking_mode: str) -> tuple:
    mode = str(ranking_mode).strip().lower()
    if mode == RANKING_MODE_BREAKOUT_PF_LOGTRADES:
        return (
            _ranking_score_breakout(row),
            row["total_net_return"],
            row["profit_factor"],
            row["mean_net_per_trade"],
            -row["threshold"],
        )
    return (
        row["mean_net_per_trade"],
        row["profit_factor"],
        row["trades"],
        -row["threshold"],
    )


def _walk_forward_sort_key(row: Dict[str, float], ranking_mode: str) -> tuple:
    mode = str(ranking_mode).strip().lower()
    if mode == RANKING_MODE_BREAKOUT_PF_LOGTRADES:
        return (
            _ranking_score_breakout(row),
            row["total_net_return"],
            row["fold_pass_ratio"],
            -row["fold_max_drawdown_pct"],
            row["profit_factor"],
            row["trades"],
            -row["threshold"],
        )
    return (
        row["fold_median_mean_net_per_trade"],
        row["fold_pass_ratio"],
        row["profit_factor"],
        row["trades"],
        -row["threshold"],
    )


def threshold_grid(min_v: float = 0.30, max_v: float = 0.90, step: float = 0.01) -> List[float]:
    vals = np.arange(float(min_v), float(max_v) + float(step) * 0.5, float(step))
    return [float(round(x, 10)) for x in vals.tolist()]


def _grid_edge_hint(grid: List[float], best_threshold: float) -> str:
    if not grid:
        return ""
    lo = float(min(grid))
    hi = float(max(grid))
    thr = float(best_threshold)
    eps = 1e-9
    if thr >= hi - eps:
        return " best candidate is at upper grid edge; consider increasing --thr-max."
    if thr <= lo + eps:
        return " best candidate is at lower grid edge; consider decreasing --thr-min."
    return ""


def choose_threshold(
    *,
    prob_valid: np.ndarray,
    ret_valid: np.ndarray,
    grid: List[float],
    cost_per_trade: float,
    min_profit_factor: float,
    max_drawdown_pct: float,
    min_trades: int,
    ranking_mode: str = RANKING_MODE_LEGACY,
) -> Dict[str, object]:
    rows = [threshold_stats(prob_valid, ret_valid, thr, cost_per_trade) for thr in grid]
    eligible = [
        r
        for r in rows
        if (r["trades"] >= int(min_trades))
        and (r["profit_factor"] >= float(min_profit_factor))
        and (r["max_drawdown_pct"] <= float(max_drawdown_pct))
    ]
    best_any = sorted(
        rows,
        key=lambda x: _single_sort_key(x, ranking_mode),
        reverse=True,
    )[0]
    if not eligible:
        edge_hint = _grid_edge_hint(grid, float(best_any["threshold"]))
        diag = threshold_diagnostics(prob_valid, ret_valid, cost_per_trade)
        raise ValueError(
            "no eligible threshold found under policy constraints: "
            f"min_profit_factor={float(min_profit_factor):.4f}, "
            f"max_drawdown_pct={float(max_drawdown_pct):.4f}, "
            f"min_trades={int(min_trades)}.\n"
            f"best_candidate(thr={float(best_any['threshold']):.4f}, "
            f"pf={float(best_any['profit_factor']):.4f}, "
            f"mdd={float(best_any['max_drawdown_pct']):.4f}, "
            f"trades={int(best_any['trades'])}, "
            f"mean_net={float(best_any['mean_net_per_trade']):.6f}).{edge_hint}\n\n"
            f"{diag}"
        )
    best = sorted(
        eligible,
        key=lambda x: _single_sort_key(x, ranking_mode),
        reverse=True,
    )[0]
    return {
        "selected_threshold": float(best["threshold"]),
        "selected_from_eligible": True,
        "ranking_mode": str(ranking_mode).strip().lower(),
        "grid_rows": rows,
        "best_row": best,
    }


def _build_fold_ids(ordered_days: np.ndarray, folds: int) -> np.ndarray:
    n = len(ordered_days)
    if n == 0:
        return np.asarray([], dtype=int)
    k = max(1, min(int(folds), n))
    cuts = np.linspace(0, n, num=k + 1, dtype=int)
    out = np.zeros(n, dtype=int)
    for i in range(k):
        out[cuts[i] : cuts[i + 1]] = i
    return out


def choose_threshold_walk_forward(
    *,
    prob_valid: np.ndarray,
    ret_valid: np.ndarray,
    day_values: np.ndarray,
    grid: List[float],
    cost_per_trade: float,
    min_profit_factor: float,
    max_drawdown_pct: float,
    min_trades: int,
    folds: int = 4,
    min_fold_pass_ratio: float = 0.75,
    ranking_mode: str = RANKING_MODE_LEGACY,
) -> Dict[str, object]:
    p = np.asarray(prob_valid, dtype=float)
    r = np.asarray(ret_valid, dtype=float)
    d = np.asarray(day_values)
    if len(p) != len(r) or len(p) != len(d):
        raise ValueError("walk-forward threshold: prob/ret/day lengths must match")
    unique_days = np.unique(d)
    if len(unique_days) < 2:
        raise ValueError("walk-forward threshold: need at least 2 unique days")
    fold_map = _build_fold_ids(unique_days, folds=folds)
    day_to_fold = {day: int(fold_map[i]) for i, day in enumerate(unique_days)}
    fold_ids = np.asarray([day_to_fold[x] for x in d], dtype=int)
    fold_count = int(np.max(fold_ids)) + 1 if len(fold_ids) else 0

    rows: List[Dict[str, object]] = []
    eligible: List[Dict[str, object]] = []
    pass_needed = max(1, int(np.ceil(float(min_fold_pass_ratio) * max(1, fold_count))))
    for thr in grid:
        all_stats = threshold_stats(p, r, thr, cost_per_trade)
        fold_stats: List[Dict[str, float]] = []
        for fid in range(fold_count):
            mask = fold_ids == fid
            fold_stats.append(threshold_stats(p[mask], r[mask], thr, cost_per_trade))
        fold_trades = np.asarray([fs["trades"] for fs in fold_stats], dtype=float)
        fold_pf = np.asarray([fs["profit_factor"] for fs in fold_stats], dtype=float)
        fold_mdd = np.asarray([fs["max_drawdown_pct"] for fs in fold_stats], dtype=float)
        fold_mean = np.asarray([fs["mean_net_per_trade"] for fs in fold_stats], dtype=float)
        pf_ok = fold_pf >= float(min_profit_factor)
        mdd_ok = fold_mdd <= float(max_drawdown_pct)
        fold_pass = pf_ok & mdd_ok
        pass_count = int(np.sum(fold_pass))
        row = {
            **all_stats,
            "folds": fold_count,
            "fold_pass_count": pass_count,
            "fold_pass_ratio": float(pass_count / max(1, fold_count)),
            "fold_min_profit_factor": float(np.min(fold_pf)) if len(fold_pf) else 0.0,
            "fold_max_drawdown_pct": float(np.max(fold_mdd)) if len(fold_mdd) else 0.0,
            "fold_median_mean_net_per_trade": float(np.median(fold_mean)) if len(fold_mean) else 0.0,
            "fold_min_trades": int(np.min(fold_trades)) if len(fold_trades) else 0,
            "fold_stats": fold_stats,
        }
        rows.append(row)
        if (
            int(all_stats["trades"]) >= int(min_trades)
            and pass_count >= pass_needed
            and row["fold_median_mean_net_per_trade"] > 0.0
        ):
            eligible.append(row)

    best_any = sorted(
        rows,
        key=lambda x: _walk_forward_sort_key(x, ranking_mode),
        reverse=True,
    )[0]
    if not eligible:
        edge_hint = _grid_edge_hint(grid, float(best_any["threshold"]))
        raise ValueError(
            "no eligible threshold found under walk-forward policy: "
            f"min_profit_factor={float(min_profit_factor):.4f}, "
            f"max_drawdown_pct={float(max_drawdown_pct):.4f}, "
            f"min_trades={int(min_trades)}, "
            f"folds={int(fold_count)}, "
            f"min_fold_pass_ratio={float(min_fold_pass_ratio):.4f}. "
            f"best_candidate(thr={float(best_any['threshold']):.4f}, "
            f"fold_pass_ratio={float(best_any['fold_pass_ratio']):.4f}, "
            f"fold_med_net={float(best_any['fold_median_mean_net_per_trade']):.6f}, "
            f"pf={float(best_any['profit_factor']):.4f}, "
            f"mdd={float(best_any['max_drawdown_pct']):.4f}, "
            f"trades={int(best_any['trades'])})."
            f"{edge_hint}"
        )
    best = sorted(
        eligible,
        key=lambda x: _walk_forward_sort_key(x, ranking_mode),
        reverse=True,
    )[0]
    return {
        "selected_threshold": float(best["threshold"]),
        "selected_from_eligible": True,
        "selection_mode": "walk_forward",
        "ranking_mode": str(ranking_mode).strip().lower(),
        "selection_meta": {
            "folds": int(fold_count),
            "min_fold_pass_ratio": float(min_fold_pass_ratio),
            "pass_folds_required": int(pass_needed),
        },
        "grid_rows": rows,
        "best_row": best,
    }
