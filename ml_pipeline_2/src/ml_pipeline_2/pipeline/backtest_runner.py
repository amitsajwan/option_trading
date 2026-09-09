"""One shared capital-weighted backtest runner, replacing the growing pile
of near-identical per-instrument scripts this session accumulated
(bn_labelgrid_bt.py, nifty_labelgrid_bt.py, sensex_v2_bt_rs.py,
fn_v3_bt_rs.py, ...) -- same logic, different instrument copy-pasted in
each time. This module is instrument-agnostic: the instrument's lot size
and other facts come from `instrument_config`, never a hardcoded constant.

`trade_rupees` / `summarize_trades` / `build_backtest_result` are pure and
unit-tested without any database or docker dependency. `run_backtest` is
the thin orchestration layer that calls `strategy_app.sim.multi_day_runner.
run_range` -- exercised on the VM (inside the strategy_app container,
same as every backtest this session), not here, since it needs live
Mongo/parquet access.

Every call to `run_backtest` enforces `validate_backtest_window` first --
this is the fix for the in-sample contamination incident: a caller simply
cannot skip the check, it's not a step someone has to remember.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .instrument_config import get_instrument_config
from .validation import DateLike, check_model_degeneracy, validate_backtest_window
from .windowing import max_drawdown_pct


def trade_rupees(trade: dict[str, Any], lot_size: int) -> float:
    """Real rupee P&L for one closed trade: (exit - entry) x lots x lot_size."""
    prem_in = float(trade.get("prem_in") or 0.0)
    prem_out = float(trade.get("prem_out") or 0.0)
    lots = int(trade.get("lots") or 1)
    return (prem_out - prem_in) * lots * lot_size


@dataclass(frozen=True)
class BacktestSummary:
    label: str
    paper_tape_trades: int
    live_trades: int
    win_rate_pct: Optional[float]
    total_capital_rs: float
    total_pnl_rs: float
    return_on_capital_pct: Optional[float]
    profit_factor: Optional[float]
    worst_trade_rs: Optional[float]
    best_trade_rs: Optional[float]
    grade_distribution: dict[str, int]
    max_drawdown_pct: Optional[float]

    @property
    def is_thin_sample(self) -> bool:
        """Statistical-rigor gate established this session: fewer than
        ~15-20 live trades isn't enough to distinguish a real result from
        noise. Callers should surface this, not just the raw percentage."""
        return self.live_trades < 15


def summarize_trades(label: str, trades: list[dict[str, Any]], lot_size: int) -> BacktestSummary:
    """Build a BacktestSummary from a run_range() result's trade list.
    Pure function -- same shape every bespoke script this session
    hand-rolled its own version of."""
    live = [t for t in trades if t.get("tier") == "live"]
    pnls_rs = [trade_rupees(t, lot_size) for t in live]
    capital_rs = [float(t.get("prem_in") or 0.0) * int(t.get("lots") or 1) * lot_size for t in live]
    wins = [p for p in pnls_rs if p > 0]
    losses = [p for p in pnls_rs if p < 0]
    total_rs = sum(pnls_rs)
    total_capital = sum(capital_rs)

    grade_distribution: dict[str, int] = {}
    for t in trades:
        g = t.get("entry_grade") or "?"
        grade_distribution[g] = grade_distribution.get(g, 0) + 1

    # Per-trade return series (pnl / that trade's own capital), in fold
    # order -- the same sequence `build_day_folds`-style walk-forward
    # analysis would consume. Trades with zero recorded capital are
    # skipped rather than divided by zero.
    per_trade_returns = [
        pnl / cap for pnl, cap in zip(pnls_rs, capital_rs) if cap
    ]

    return BacktestSummary(
        label=label,
        paper_tape_trades=len(trades),
        live_trades=len(live),
        win_rate_pct=(100.0 * len(wins) / len(live)) if live else None,
        total_capital_rs=total_capital,
        total_pnl_rs=total_rs,
        return_on_capital_pct=(100.0 * total_rs / total_capital) if total_capital else None,
        profit_factor=(sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else None),
        worst_trade_rs=min(pnls_rs) if pnls_rs else None,
        best_trade_rs=max(pnls_rs) if pnls_rs else None,
        grade_distribution=grade_distribution,
        max_drawdown_pct=(100.0 * max_drawdown_pct(per_trade_returns)) if per_trade_returns else None,
    )


def build_backtest_result(summary: BacktestSummary) -> dict[str, Any]:
    """Convert a BacktestSummary into the plain dict shape
    registry.build_run_document expects for its `result` field."""
    return {
        "paper_tape_trades": summary.paper_tape_trades,
        "live_trades": summary.live_trades,
        "win_rate_pct": summary.win_rate_pct,
        "total_capital_rs": summary.total_capital_rs,
        "total_pnl_rs": summary.total_pnl_rs,
        "return_on_capital_pct": summary.return_on_capital_pct,
        "profit_factor": summary.profit_factor,
        "worst_trade_rs": summary.worst_trade_rs,
        "best_trade_rs": summary.best_trade_rs,
        "grade_distribution": summary.grade_distribution,
        "max_drawdown_pct": summary.max_drawdown_pct,
        "is_thin_sample": summary.is_thin_sample,
    }


def run_backtest(
    *,
    instrument: str,
    model_path: str,
    entry_min_prob: float,
    date_from: DateLike,
    date_to: DateLike,
    holdout_end: DateLike,
    label: Optional[str] = None,
    extra_config: Optional[dict[str, str]] = None,
    min_gap_days: int = 0,
    holdout_eval: Optional[dict[str, Any]] = None,
    run_range_fn: Optional[Callable[..., Any]] = None,
) -> BacktestSummary:
    """Run one capital-weighted backtest config for an instrument.

    ALWAYS validates the window against holdout_end first (raises
    BacktestWindowContaminated if it overlaps) -- this cannot be skipped
    by a caller the way the bespoke scripts this session could (and once
    did) skip it.

    If `holdout_eval` is supplied (the trained bundle's own holdout_eval
    dict), ALSO checks that `entry_min_prob` is one of this model's own
    usable thresholds before spending time on the backtest -- catches a
    FINNIFTY-style "threshold above the model's real ceiling" mistake
    before it wastes a multi-hour run, not just after. Pass it whenever
    the model bundle is available; omit only when testing the plumbing
    itself.

    `run_range_fn` is injectable for testing; defaults to the real
    `strategy_app.sim.multi_day_runner.run_range`, which requires live
    Mongo/parquet access and is only actually callable inside the
    strategy_app container on the VM.
    """
    validate_backtest_window(
        holdout_end=holdout_end, backtest_from=date_from, backtest_to=date_to,
        min_gap_days=min_gap_days,
    )
    if holdout_eval is not None:
        check_model_degeneracy(holdout_eval, chosen_threshold=entry_min_prob)

    cfg = get_instrument_config(instrument)
    config_env = {
        "ML_ENTRY_DIRECTION_MODE": "composite",
        "ENTRY_DIR_W_ML": "0",
        "RISK_LIVE_MIN_GRADE": "GOOD",
        "STRATEGY_INSTRUMENT": cfg.name,
        "STRATEGY_LOT_SIZE": str(cfg.lot_size),
        "ENTRY_ML_MODEL_PATH": model_path,
        "ENTRY_ML_MIN_PROB": str(entry_min_prob),
        **(extra_config or {}),
    }

    if run_range_fn is None:
        from strategy_app.sim.multi_day_runner import run_range as run_range_fn  # type: ignore[no-redef]

    result = run_range_fn(str(date_from), str(date_to), config_env)
    all_trades = [t for day in result.days for t in day.trades]
    return summarize_trades(label or f"{cfg.name} {model_path} @thr={entry_min_prob}", all_trades, cfg.lot_size)
