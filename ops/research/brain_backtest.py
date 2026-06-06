"""Cost-aware end-to-end brain backtest (board B-2.6 — the GO/NO-GO gate).

Runs the full Intelligent Brain (senses -> DecisionBrain) over live days and reports
net P&L as a SENSITIVITY CURVE over assumed direction accuracy (50/55/58/60/perfect),
because direction is the deferred Sprint-4 component (Decision D5). Every trade is
costed through ``cost_model.py`` (D4 — no 6 bps anywhere). Asserts per-bar latency
< 1s with no LLM on the path (D6).

Gate read (D5): PASS if portfolio_net(P_REF) >= 0, OR the curve crosses zero at an
achievable accuracy (<= ~0.60) with portfolio_net(perfect) > 0. STOP only if
portfolio_net(perfect) < 0 (then move/destination/cost is the problem, not direction).

RUN on the VM (real numbers need ``trading_ai.phase1_market_snapshots``):
    sudo docker exec <strategy_app> python /tmp/brain_backtest.py
Locally it has no data; use ``run_brain_backtest(days_bars=...)`` from tests.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from strategy_app.brain.decision_brain import CURVE_POINTS, P_REF, DecisionBrain
from strategy_app.brain.sense_runner import run_senses
from strategy_app.senses.context import build_contexts
from strategy_app.senses.cost_ev import CostEvSense
from strategy_app.senses.direction import PlaceholderDirection

HORIZON = 10


@dataclass
class BacktestReport:
    days: int
    bars: int
    trades: int
    accountable_trades: int
    net_curve: dict[float, float]          # portfolio net % (sum over trades) per accuracy point
    avg_net_curve: dict[float, float]      # per-trade average
    latency_ms_p99: float
    latency_ms_max: float
    reason_counts: dict[str, int] = field(default_factory=dict)

    #: direction accuracy we have plausibly achieved (handover: structural/ML ~0.55-0.59)
    ACHIEVABLE_ACCURACY = 0.60

    def breakeven_accuracy(self) -> float | None:
        """Interpolate the zero-crossing of the (linear) net-vs-accuracy curve."""
        pts = sorted(self.net_curve.items())
        for (p_lo, n_lo), (p_hi, n_hi) in zip(pts, pts[1:]):
            if n_lo < 0 <= n_hi and n_hi != n_lo:
                return p_lo + (0.0 - n_lo) / (n_hi - n_lo) * (p_hi - p_lo)
        if pts[0][1] >= 0:
            return pts[0][0]      # already +EV at the lowest sampled accuracy
        return None               # never crosses zero within the sampled range

    def gate(self) -> str:
        perfect = self.net_curve.get(1.0, 0.0)
        at_ref = self.net_curve.get(P_REF, 0.0)
        if self.accountable_trades == 0:
            return "NO-TRADES (inconclusive — widen data or thresholds)"
        if perfect < 0:
            return "STOP (negative even with perfect direction — move/destination/cost is the problem)"
        if at_ref >= 0:
            return "PASS (net>=0 at realistic structural-bias direction)"
        be = self.breakeven_accuracy()
        if be is None:
            return "STOP (never breaks even within sampled accuracy)"
        if be <= self.ACHIEVABLE_ACCURACY:
            return f"PASS (direction is the only gap — breaks even at achievable accuracy {be:.3f})"
        return (f"MARGINAL (breaks even at {be:.3f} > achievable ~{self.ACHIEVABLE_ACCURACY:.2f}; "
                f"profitable with perfect direction, so not a STOP — direction is the gap)")

    def render(self) -> str:
        lines = [
            f"BrainBacktest | days={self.days} bars={self.bars} trades={self.trades} "
            f"(accountable={self.accountable_trades}) | latency p99={self.latency_ms_p99:.2f}ms "
            f"max={self.latency_ms_max:.2f}ms",
            "",
            "Net P&L vs assumed direction accuracy (% of premium, summed over trades):",
            f"{'accuracy':>10} {'portfolio_net%':>15} {'avg/trade%':>12}",
        ]
        for p in sorted(self.net_curve):
            lines.append(f"{p:>10.2f} {self.net_curve[p] * 100:>14.2f}% {self.avg_net_curve[p] * 100:>11.2f}%")
        lines += ["", f"GATE: {self.gate()}", "",
                  "Decision reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(self.reason_counts.items()))]
        return "\n".join(lines)


def run_brain_backtest(
    days_bars: dict[str, list[dict[str, Any]]],
    *,
    levels: dict[str, dict[str, float | None]] | None = None,
    p_ref: float = P_REF,
    direction_side: str = "CE",
    cost_ev: CostEvSense | None = None,
    horizon: int = HORIZON,
    latency_budget_ms: float = 1000.0,
) -> BacktestReport:
    cost_ev = cost_ev or CostEvSense()
    dir_s = PlaceholderDirection(direction_side)
    brain = DecisionBrain(p_ref=p_ref, defer_direction=True)

    contexts = build_contexts(days_bars, horizon=horizon, levels=levels)
    curve_sum = {p: 0.0 for p in CURVE_POINTS}
    trades = accountable = 0
    latencies: list[float] = []
    reason_counts: dict[str, int] = {}
    cooldown_until: dict[str, int] = {}

    for ctx in contexts:
        base = ctx.as_mapping()
        in_position = ctx.index <= cooldown_until.get(ctx.day, -1)
        base["in_position"] = in_position

        verdicts = run_senses(base, cost_ev=cost_ev, direction_sense=dir_s)

        t0 = perf_counter()
        decision = brain.decide(verdicts)
        latencies.append((perf_counter() - t0) * 1000.0)

        reason_counts[decision.action] = reason_counts.get(decision.action, 0) + 1
        if decision.action != "TRADE":
            continue
        trades += 1
        cooldown_until[ctx.day] = ctx.index + horizon

        # P&L accounting on the REALISED move (gate decided on expected edge above).
        realised = ctx.future_move_pt
        if realised is None:
            continue
        accountable += 1
        right = cost_ev.right_at(float(realised))
        wrong = cost_ev.wrong_at(float(realised))
        cost = cost_ev.cost_pct()
        for p in CURVE_POINTS:
            curve_sum[p] += p * right + (1.0 - p) * wrong - cost

    latencies.sort()
    p99 = latencies[min(len(latencies) - 1, int(0.99 * len(latencies)))] if latencies else 0.0
    lat_max = latencies[-1] if latencies else 0.0
    assert lat_max < latency_budget_ms, f"latency budget blown: {lat_max:.1f}ms >= {latency_budget_ms}ms (D6)"

    avg_curve = {p: (curve_sum[p] / accountable if accountable else 0.0) for p in CURVE_POINTS}
    return BacktestReport(
        days=len(days_bars), bars=len(contexts), trades=trades, accountable_trades=accountable,
        net_curve={p: round(curve_sum[p], 5) for p in CURVE_POINTS},
        avg_net_curve={p: round(avg_curve[p], 5) for p in CURVE_POINTS},
        latency_ms_p99=round(p99, 3), latency_ms_max=round(lat_max, 3),
        reason_counts=reason_counts,
    )


# ---- mongo loader (VM) ----

def _load_days_from_mongo() -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, float | None]]]:
    from pymongo import MongoClient

    host = os.getenv("MONGO_HOST", "mongo")
    db = os.getenv("MONGO_DB", "trading_ai")
    coll = MongoClient(f"mongodb://{host}:27017")[db][os.getenv("BIGMOVE_SOURCE_COLL", "phase1_market_snapshots")]
    explicit = [d.strip() for d in os.getenv("BIGMOVE_DAYS", "").split(",") if d.strip()]
    days = explicit or sorted(str(d) for d in coll.distinct("trade_date_ist") if d)
    days_bars: dict[str, list[dict[str, Any]]] = {}
    for day in days:
        rows = []
        for d in coll.find({"trade_date_ist": day}).sort("timestamp", 1):
            s = (d.get("payload") or {}).get("snapshot") or {}
            f = s.get("futures_bar") or {}
            ca = s.get("chain_aggregates") or {}
            orng = s.get("opening_range") or {}
            rows.append({
                "c": f.get("fut_close"), "h": f.get("fut_high"), "l": f.get("fut_low"),
                "ovol": (ca.get("total_ce_volume") or 0) + (ca.get("total_pe_volume") or 0),
                "ooi": (ca.get("total_ce_oi") or 0) + (ca.get("total_pe_oi") or 0),
                "max_pain": ca.get("max_pain"),
                "ce_oi_top_strike": ca.get("ce_oi_top_strike"), "pe_oi_top_strike": ca.get("pe_oi_top_strike"),
                "opening_range_high": orng.get("high"), "opening_range_low": orng.get("low"),
            })
        days_bars[day] = rows
    # prior-day high/low as always-available levels
    levels: dict[str, dict[str, float | None]] = {}
    prev_hi = prev_lo = None
    for day in days:
        levels[day] = {"prior_day_high": prev_hi, "prior_day_low": prev_lo}
        highs = [r["h"] for r in days_bars[day] if r["h"] is not None]
        lows = [r["l"] for r in days_bars[day] if r["l"] is not None]
        prev_hi = max(highs) if highs else prev_hi
        prev_lo = min(lows) if lows else prev_lo
    return days_bars, levels


def main() -> None:
    days_bars, levels = _load_days_from_mongo()
    report = run_brain_backtest(days_bars, levels=levels)
    print(report.render())


if __name__ == "__main__":
    main()
