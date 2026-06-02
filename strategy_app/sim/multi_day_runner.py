"""Multi-day sim runner and aggregator (MD-S3 + MD-S4).

Usage (CLI, MD-S5)
------------------
    python -m strategy_app.sim.multi_day_runner \
        --from 2025-01-01 --to 2025-03-31 \
        [--config EXIT_STRATEGY_MODE=scalper,RISK_MAX_SESSION_TRADES=12] \
        [--ab EXIT_STRATEGY_MODE=lottery] \
        [--out docs/reports/]

Programmatic
------------
    from strategy_app.sim.multi_day_runner import run_range, ab_compare

Public API
----------
run_range(date_from, date_to, config_env, parquet_base) -> MultiDayResult
    Replay every trading day in range; return aggregate + per-day table.

ab_compare(date_from, date_to, config_a, config_b, parquet_base) -> ABResult
    Run the same day set under two configs; return side-by-side comparison.

Metrics defined in docs/strategy_platform/02_MULTI_DAY_SIM.md §4.
All returns are simple-sum (not compounded) and pre-cost unless
TRANSACTION_COST_PER_LOT is set in the config_env.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class DayResult:
    trade_date:    str
    trades:        List[dict]
    pnl:           float          # simple sum of trade pnl_pct
    win_count:     int
    trade_count:   int
    profit_factor: float          # Σwin / |Σloss|; inf when no losses
    expectancy:    float          # mean trade pnl_pct
    avg_mfe:       float
    capture_ratio: float          # mean(pnl/mfe) for trades with mfe>0; NaN when none
    error:         Optional[str] = None   # non-None when day failed to replay


@dataclass
class MultiDayResult:
    date_from:       str
    date_to:         str
    config_env:      Dict[str, str]
    days:            List[DayResult]

    # Aggregate metrics
    cumulative_pnl:  float = 0.0
    win_days:        int   = 0
    total_days:      int   = 0
    total_trades:    int   = 0
    total_wins:      int   = 0
    expectancy:      float = 0.0    # mean trade pnl across ALL trades
    profit_factor:   float = 0.0    # Σall wins / |Σall losses|
    max_drawdown:    float = 0.0    # max peak-to-trough decline of daily cumulative curve
    daily_returns:   List[float] = field(default_factory=list)
    fat_tail_days:   int = 0        # days with daily_return > fat_tail_threshold
    fat_tail_threshold: float = 0.05  # 5% default

    error_days:      int = 0
    exit_stack_name: str = ""


@dataclass
class ABResult:
    date_from:  str
    date_to:    str
    result_a:   MultiDayResult
    result_b:   MultiDayResult
    winner_pnl: str   # "A", "B", or "tie"
    winner_dd:  str   # "A" (lower dd), "B", or "tie"


# ── Runner ────────────────────────────────────────────────────────────────────

_ENV_LOCK = threading.Lock()


def run_range(
    date_from: str,
    date_to: str,
    config_env: Optional[Dict[str, str]] = None,
    parquet_base: Optional[str] = None,
    fat_tail_threshold: float = 0.05,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> MultiDayResult:
    """Replay every available trading day between date_from and date_to (inclusive).

    Args:
        date_from:         "YYYY-MM-DD" start date (inclusive).
        date_to:           "YYYY-MM-DD" end date (inclusive).
        config_env:        Dict of env var overrides applied for this run (layered on top
                           of the live ops_env.json baseline if it exists; caller is
                           responsible for loading the baseline first).
        parquet_base:      Path to parquet data root.
        fat_tail_threshold: Daily return threshold for counting fat-tail days (default 5%).
        progress_cb:       Optional callable(trade_date, day_index, total_days) for UI progress.

    Returns:
        MultiDayResult with per-day results and aggregated metrics.

    Fidelity rules:
        - Fresh engine per day (no state bleed).
        - STRATEGY_RUN_DIR forced to an isolated /tmp path.
        - STRATEGY_REDIS_PUBLISH_ENABLED=0.
        - config_env must include ML library-compatible settings (caller responsibility).
    """
    from strategy_app.sim.snapshot_loader import available_days, load_day
    from strategy_app.sim.replay_engine import replay_day

    days = available_days(parquet_base, date_from=date_from, date_to=date_to)
    if not days:
        logger.warning("multi_day_runner: no snapshot days found in [%s, %s]", date_from, date_to)

    result = MultiDayResult(
        date_from=date_from,
        date_to=date_to,
        config_env=dict(config_env or {}),
        days=[],
        fat_tail_threshold=fat_tail_threshold,
    )

    for idx, trade_date in enumerate(days):
        if progress_cb is not None:
            progress_cb(trade_date, idx, len(days))

        try:
            day = _replay_one_day(
                trade_date=trade_date,
                config_env=config_env or {},
                parquet_base=parquet_base,
                load_day_fn=load_day,
                replay_day_fn=replay_day,
            )
        except Exception as exc:
            logger.exception("multi_day_runner: failed day %s: %s", trade_date, exc)
            day = DayResult(
                trade_date=trade_date,
                trades=[],
                pnl=0.0,
                win_count=0,
                trade_count=0,
                profit_factor=0.0,
                expectancy=0.0,
                avg_mfe=0.0,
                capture_ratio=float("nan"),
                error=str(exc),
            )
            result.error_days += 1

        result.days.append(day)
        if day.exit_stack_name if hasattr(day, "exit_stack_name") else False:
            result.exit_stack_name = getattr(day, "exit_stack_name", "")

    _aggregate(result)
    return result


def _replay_one_day(
    trade_date: str,
    config_env: Dict[str, str],
    parquet_base: Optional[str],
    load_day_fn,
    replay_day_fn,
) -> DayResult:
    snapshots = load_day_fn(trade_date, parquet_base)
    if not snapshots:
        return DayResult(
            trade_date=trade_date,
            trades=[],
            pnl=0.0, win_count=0, trade_count=0,
            profit_factor=0.0, expectancy=0.0, avg_mfe=0.0,
            capture_ratio=float("nan"),
            error="no_snapshots",
        )

    run_id = f"sim-{trade_date}"
    sim_env = {
        "STRATEGY_RUN_ID": run_id,
        "STRATEGY_REDIS_PUBLISH_ENABLED": "0",
        "MARKET_SESSION_ENABLED": "0",
        "BRAIN_ENABLED": "false",
        "STRATEGY_STARTUP_WARMUP_EVENTS": "0",
        "DEPTH_FEED_ENABLED": "0",
    }
    sim_env.update(config_env)

    with tempfile.TemporaryDirectory(prefix=f"sim_{trade_date}_") as tmpdir:
        sim_env["STRATEGY_RUN_DIR"] = tmpdir

        with _ENV_LOCK:
            old_env: Dict[str, Optional[str]] = {}
            for k, v in sim_env.items():
                old_env[k] = os.environ.get(k)
                os.environ[k] = v
            try:
                replay_result = replay_day_fn(snapshots, trade_date)
            finally:
                for k, old in old_env.items():
                    if old is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = old

    trades = replay_result["trades"]
    exit_stack_name = replay_result["exit_stack_name"]
    return _make_day_result(trade_date, trades, exit_stack_name)


def _make_day_result(trade_date: str, trades: List[dict], exit_stack_name: str = "") -> DayResult:
    pnls = [float(t.get("pnl_pct", 0)) for t in trades]
    mfes = [float(t.get("mfe_pct", 0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    caps = [p / m for p, m in zip(pnls, mfes) if m and m > 0]

    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")

    day = DayResult(
        trade_date=trade_date,
        trades=trades,
        pnl=sum(pnls),
        win_count=len(wins),
        trade_count=len(pnls),
        profit_factor=pf,
        expectancy=sum(pnls) / len(pnls) if pnls else 0.0,
        avg_mfe=sum(mfes) / len(mfes) if mfes else 0.0,
        capture_ratio=sum(caps) / len(caps) if caps else float("nan"),
    )
    return day


def _aggregate(result: MultiDayResult) -> None:
    """Compute portfolio-level metrics from per-day results in place."""
    all_pnls: List[float] = []
    all_wins: List[float] = []
    all_losses: List[float] = []

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0

    for day in result.days:
        if day.error and day.error != "no_snapshots":
            continue
        result.total_days += 1
        result.total_trades += day.trade_count
        result.total_wins += day.win_count
        result.daily_returns.append(day.pnl)
        cumulative += day.pnl
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_dd:
            max_dd = drawdown
        if day.pnl > result.fat_tail_threshold:
            result.fat_tail_days += 1
        if day.pnl > 0:
            result.win_days += 1
        for t in day.trades:
            p = float(t.get("pnl_pct", 0))
            all_pnls.append(p)
            if p > 0:
                all_wins.append(p)
            elif p < 0:
                all_losses.append(p)

    result.cumulative_pnl = cumulative
    result.max_drawdown = max_dd
    result.expectancy = sum(all_pnls) / len(all_pnls) if all_pnls else 0.0
    result.profit_factor = (
        sum(all_wins) / abs(sum(all_losses)) if all_losses else float("inf")
    )


# ── A/B harness (MD-S4) ───────────────────────────────────────────────────────

def ab_compare(
    date_from: str,
    date_to: str,
    config_a: Dict[str, str],
    config_b: Dict[str, str],
    parquet_base: Optional[str] = None,
    fat_tail_threshold: float = 0.05,
) -> ABResult:
    """Run two configs over the identical day set; return side-by-side aggregates.

    Both configs are run on exactly the same list of available days so the comparison
    is apples-to-apples — no silent date-mismatch.
    """
    logger.info("AB compare: running config A over [%s, %s]", date_from, date_to)
    result_a = run_range(date_from, date_to, config_a, parquet_base, fat_tail_threshold)

    logger.info("AB compare: running config B over [%s, %s]", date_from, date_to)
    result_b = run_range(date_from, date_to, config_b, parquet_base, fat_tail_threshold)

    winner_pnl: str
    if abs(result_a.cumulative_pnl - result_b.cumulative_pnl) < 1e-6:
        winner_pnl = "tie"
    elif result_a.cumulative_pnl > result_b.cumulative_pnl:
        winner_pnl = "A"
    else:
        winner_pnl = "B"

    winner_dd: str
    if abs(result_a.max_drawdown - result_b.max_drawdown) < 1e-6:
        winner_dd = "tie"
    elif result_a.max_drawdown < result_b.max_drawdown:
        winner_dd = "A"
    else:
        winner_dd = "B"

    return ABResult(
        date_from=date_from,
        date_to=date_to,
        result_a=result_a,
        result_b=result_b,
        winner_pnl=winner_pnl,
        winner_dd=winner_dd,
    )


# ── Markdown report (MD-S5) ──────────────────────────────────────────────────

def render_report(result: MultiDayResult, ab: Optional[ABResult] = None) -> str:
    """Render a Markdown report from a MultiDayResult (and optional A/B comparison).

    Output is deterministic for a fixed result object, so it can be committed to
    docs/reports/ and regenerated identically.
    """
    lines: List[str] = []

    def _h(n: int, text: str) -> None:
        lines.append(f"{'#' * n} {text}")
        lines.append("")

    def _row(*cols) -> str:
        return "| " + " | ".join(str(c) for c in cols) + " |"

    _h(1, "Multi-Day Sim Report")
    lines.append(f"Period: **{result.date_from}** → **{result.date_to}**")
    lines.append(f"Config: `{_fmt_config(result.config_env)}`")
    lines.append("")

    _h(2, "Summary")
    lines.append(_row("Metric", "Value"))
    lines.append(_row("---", "---"))
    lines.append(_row("Trading days", result.total_days))
    lines.append(_row("Win days", f"{result.win_days} ({_pct(result.win_days, result.total_days)})"))
    lines.append(_row("Total trades", result.total_trades))
    lines.append(_row("Win trades", f"{result.total_wins} ({_pct(result.total_wins, result.total_trades)})"))
    lines.append(_row("Cumulative P&L", f"{result.cumulative_pnl:+.2f}%"))
    lines.append(_row("Expectancy (per trade)", f"{result.expectancy:+.4f}%"))
    lines.append(_row("Profit factor", f"{result.profit_factor:.2f}" if math.isfinite(result.profit_factor) else "∞"))
    lines.append(_row("Max drawdown", f"{result.max_drawdown:.2f}%"))
    lines.append(_row("Fat-tail days (>{:.0f}%)".format(result.fat_tail_threshold * 100), result.fat_tail_days))
    lines.append(_row("Error days", result.error_days))
    lines.append("")

    if ab is not None:
        _h(2, "A/B Comparison")
        a, b = ab.result_a, ab.result_b
        lines.append(_row("Metric", "Config A", "Config B", "Winner"))
        lines.append(_row("---", "---", "---", "---"))
        lines.append(_row("Exit mode", _get_cfg(a, "EXIT_STRATEGY_MODE"), _get_cfg(b, "EXIT_STRATEGY_MODE"), ""))
        lines.append(_row("Cum P&L", f"{a.cumulative_pnl:+.2f}%", f"{b.cumulative_pnl:+.2f}%", ab.winner_pnl))
        lines.append(_row("Max DD", f"{a.max_drawdown:.2f}%", f"{b.max_drawdown:.2f}%", ab.winner_dd))
        lines.append(_row("Profit factor",
                          f"{a.profit_factor:.2f}" if math.isfinite(a.profit_factor) else "∞",
                          f"{b.profit_factor:.2f}" if math.isfinite(b.profit_factor) else "∞", ""))
        lines.append(_row("Win rate (days)",
                          _pct(a.win_days, a.total_days),
                          _pct(b.win_days, b.total_days), ""))
        lines.append(_row("Trades/day",
                          f"{a.total_trades/max(a.total_days,1):.1f}",
                          f"{b.total_trades/max(b.total_days,1):.1f}", ""))
        lines.append(_row("Fat-tail days", a.fat_tail_days, b.fat_tail_days, ""))
        lines.append("")

    _h(2, "Per-Day Results")
    lines.append(_row("Date", "Trades", "PnL%", "Win%", "PF", "Max-DD contrib"))
    lines.append(_row("---", "---", "---", "---", "---", "---"))
    for day in result.days:
        pf_str = f"{day.profit_factor:.2f}" if math.isfinite(day.profit_factor) else "∞"
        lines.append(_row(
            day.trade_date,
            day.trade_count,
            f"{day.pnl:+.2f}%",
            _pct(day.win_count, day.trade_count),
            pf_str,
            day.error or "",
        ))
    lines.append("")

    return "\n".join(lines)


def _pct(a: int, b: int) -> str:
    if not b:
        return "—"
    return f"{100 * a / b:.0f}%"


def _fmt_config(cfg: Dict[str, str]) -> str:
    if not cfg:
        return "(live defaults)"
    return ", ".join(f"{k}={v}" for k, v in sorted(cfg.items()))


def _get_cfg(r: MultiDayResult, key: str) -> str:
    return r.config_env.get(key, "—")


# ── CLI entry point (MD-S5) ──────────────────────────────────────────────────

def _cli() -> None:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Multi-day sim runner")
    parser.add_argument("--from", dest="date_from", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--to",   dest="date_to",   required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--config", default="", help="Comma-separated KEY=VALUE env overrides")
    parser.add_argument("--ab",     default="", help="Second config for A/B; same format as --config")
    parser.add_argument("--out",    default="docs/reports/", help="Output directory for report")
    parser.add_argument("--parquet-base", default=None, help="Override PARQUET_BASE")
    args = parser.parse_args()

    def _parse_cfg(s: str) -> Dict[str, str]:
        cfg: Dict[str, str] = {}
        for item in s.split(","):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                cfg[k.strip()] = v.strip()
        return cfg

    config_a = _parse_cfg(args.config)
    result_a = run_range(
        args.date_from, args.date_to, config_a,
        parquet_base=args.parquet_base,
        progress_cb=lambda d, i, n: print(f"  [{i+1}/{n}] {d}", flush=True),
    )

    ab: Optional[ABResult] = None
    if args.ab:
        config_b = _parse_cfg(args.ab)
        ab = ab_compare(args.date_from, args.date_to, config_a, config_b,
                        parquet_base=args.parquet_base)

    report = render_report(result_a, ab=ab)
    print(report)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"multiday_{args.date_from}_{args.date_to}.md"
    (out_dir / fname).write_text(report, encoding="utf-8")
    print(f"\nReport written to {out_dir / fname}")


if __name__ == "__main__":
    _cli()
