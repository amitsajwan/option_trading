"""Offline strategy research runner over historical snapshot parquet."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.snapshot_access import (
    DEFAULT_HISTORICAL_PARQUET_BASE,
    SNAPSHOT_DATASET_CANONICAL,
    SNAPSHOT_INPUT_MODE_CANONICAL,
    require_snapshot_access,
)
from snapshot_app.historical.window_manifest import (
    DEFAULT_MIN_TRADING_DAYS,
    DEFAULT_REQUIRED_SCHEMA_VERSION,
    load_and_validate_window_manifest,
    split_boundaries_for_days,
)
from strategy_app.contracts import PositionContext, StrategyVote, TradeSignal
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine

DEFAULT_PARQUET_BASE = DEFAULT_HISTORICAL_PARQUET_BASE
DEFAULT_OUTPUT_ROOT = Path(".run/strategy_research")
DEFAULT_CAPITAL = 500000.0
BANKNIFTY_LOT_SIZE = 15
DEFAULT_BROKERAGE_PER_ORDER = 20.0
DEFAULT_CHARGES_BPS_PER_SIDE = 2.5
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 7.5
_REASON_RE = re.compile(r"^\[(?P<regime>[^\]]+)\]\s+(?P<strategy>[^:]+):")


def _safe_ratio(numerator: float, denominator: float) -> Optional[float]:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def _parse_reason(reason: str) -> tuple[Optional[str], Optional[str]]:
    match = _REASON_RE.match(str(reason or "").strip())
    if not match:
        return None, None
    return (
        str(match.group("strategy") or "").strip() or None,
        str(match.group("regime") or "").strip() or None,
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    risk_config: dict[str, Any]


@dataclass(frozen=True)
class TradingCostModel:
    brokerage_per_order: float = DEFAULT_BROKERAGE_PER_ORDER
    charges_bps_per_side: float = DEFAULT_CHARGES_BPS_PER_SIDE
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE

    def breakdown(self, *, entry_value: float, exit_value: float) -> dict[str, float]:
        safe_entry = max(0.0, float(entry_value))
        safe_exit = max(0.0, float(exit_value))
        brokerage = 2.0 * max(0.0, float(self.brokerage_per_order))
        charges_rate = max(0.0, float(self.charges_bps_per_side)) / 10000.0
        slippage_rate = max(0.0, float(self.slippage_bps_per_side)) / 10000.0
        charges = (safe_entry + safe_exit) * charges_rate
        slippage = (safe_entry + safe_exit) * slippage_rate
        total = brokerage + charges + slippage
        return {
            "brokerage_cost_amount": brokerage,
            "charges_cost_amount": charges,
            "slippage_cost_amount": slippage,
            "total_cost_amount": total,
        }

    def to_metadata(self) -> dict[str, float]:
        return {
            "brokerage_per_order": float(self.brokerage_per_order),
            "charges_bps_per_side": float(self.charges_bps_per_side),
            "slippage_bps_per_side": float(self.slippage_bps_per_side),
        }


class MemorySignalLogger:
    """In-memory logger used for offline backtest analysis."""

    def __init__(self, *, capital_allocated: float, cost_model: Optional[TradingCostModel] = None) -> None:
        self._capital_allocated = float(capital_allocated)
        self._cost_model = cost_model or TradingCostModel()
        self._run_id: Optional[str] = None
        self._open_positions: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.vote_counts: dict[str, int] = {}

    def set_run_context(self, run_id: Optional[str]) -> None:
        text = str(run_id or "").strip()
        self._run_id = text or None

    def log_vote(self, vote: StrategyVote) -> None:
        self.vote_counts[vote.strategy_name] = self.vote_counts.get(vote.strategy_name, 0) + 1

    def log_signal(self, signal: TradeSignal, *, acted_on: bool = True) -> None:  # noqa: ARG002
        return

    def log_position_open(self, signal: TradeSignal, position: PositionContext) -> None:
        strategy, regime = _parse_reason(signal.reason)
        chosen_vote = signal.votes[0] if signal.votes else None
        self._open_positions[position.position_id] = {
            "position_id": position.position_id,
            "run_id": self._run_id,
            "entry_time": signal.timestamp,
            "trade_date": signal.timestamp.date().isoformat(),
            "year": signal.timestamp.year,
            "month": signal.timestamp.month,
            "weekday": signal.timestamp.strftime("%A"),
            "direction": position.direction,
            "strike": position.strike,
            "entry_premium": position.entry_premium,
            "lots": position.lots,
            "entry_strategy": position.entry_strategy or strategy,
            "regime": (
                (chosen_vote.raw_signals.get("_regime") if chosen_vote and isinstance(chosen_vote.raw_signals, dict) else None)
                or regime
            ),
            "entry_reason": signal.reason,
            "confidence": signal.confidence,
            "stop_loss_pct": position.stop_loss_pct,
            "target_pct": position.target_pct,
            "trailing_enabled": position.trailing_enabled,
            "trailing_activation_pct": position.trailing_activation_pct,
            "trailing_offset_pct": position.trailing_offset_pct,
            "trailing_lock_breakeven": position.trailing_lock_breakeven,
            "orb_trail_activation_mfe": position.orb_trail_activation_mfe,
            "orb_trail_offset_pct": position.orb_trail_offset_pct,
            "orb_trail_min_lock_pct": position.orb_trail_min_lock_pct,
            "orb_trail_priority_over_regime": position.orb_trail_priority_over_regime,
            "orb_trail_regime_filter": position.orb_trail_regime_filter,
            "oi_trail_activation_mfe": position.oi_trail_activation_mfe,
            "oi_trail_offset_pct": position.oi_trail_offset_pct,
            "oi_trail_min_lock_pct": position.oi_trail_min_lock_pct,
            "oi_trail_priority_over_regime": position.oi_trail_priority_over_regime,
            "oi_trail_regime_filter": position.oi_trail_regime_filter,
            "entry_stop_price": position.stop_price,
        }

    def log_position_manage(self, *, position: PositionContext, timestamp: datetime, snapshot_id: str) -> None:  # noqa: ARG002
        return

    def log_decision_trace(self, trace: dict[str, Any]) -> None:  # noqa: ARG002
        return

    def log_position_close(
        self,
        *,
        exit_signal: TradeSignal,
        position: Optional[PositionContext] = None,  # noqa: ARG002
        entry_premium: float,
        exit_premium: float,
        pnl_pct: float,
        mfe_pct: float,
        mae_pct: float,
        bars_held: int,
        stop_loss_pct: float,
        stop_price: Optional[float],
        high_water_premium: float,
        target_pct: float,
        trailing_enabled: bool,
        trailing_activation_pct: float,
        trailing_offset_pct: float,
        trailing_lock_breakeven: bool,
        trailing_active: bool,
        orb_trail_activation_mfe: float,
        orb_trail_offset_pct: float,
        orb_trail_min_lock_pct: float,
        orb_trail_priority_over_regime: bool,
        orb_trail_regime_filter: Optional[str],
        orb_trail_active: bool,
        orb_trail_stop_price: Optional[float],
        oi_trail_activation_mfe: float,
        oi_trail_offset_pct: float,
        oi_trail_min_lock_pct: float,
        oi_trail_priority_over_regime: bool,
        oi_trail_regime_filter: Optional[str],
        oi_trail_active: bool,
        oi_trail_stop_price: Optional[float],
    ) -> None:
        row = dict(self._open_positions.pop(str(exit_signal.position_id), {}))
        lots = int(row.get("lots") or 1)
        entry_value = float(entry_premium) * lots * BANKNIFTY_LOT_SIZE
        exit_value = float(exit_premium) * lots * BANKNIFTY_LOT_SIZE
        gross_pnl_amount = float(pnl_pct) * float(entry_premium) * lots * BANKNIFTY_LOT_SIZE
        cost_breakdown = self._cost_model.breakdown(entry_value=entry_value, exit_value=exit_value)
        total_cost_amount = float(cost_breakdown["total_cost_amount"])
        net_pnl_amount = gross_pnl_amount - total_cost_amount
        capital_pnl_gross = (gross_pnl_amount / self._capital_allocated) if self._capital_allocated > 0 else 0.0
        capital_pnl_net = (net_pnl_amount / self._capital_allocated) if self._capital_allocated > 0 else 0.0
        pnl_pct_net = (net_pnl_amount / entry_value) if entry_value > 0 else 0.0
        row.update(
            {
                "exit_time": exit_signal.timestamp,
                "exit_reason": exit_signal.exit_reason.value if exit_signal.exit_reason else None,
                "exit_reason_detail": exit_signal.reason,
                "entry_premium": entry_premium,
                "exit_premium": exit_premium,
                "pnl_pct": pnl_pct,
                "pnl_pct_gross": pnl_pct,
                "pnl_pct_net": pnl_pct_net,
                "entry_value_amount": entry_value,
                "exit_value_amount": exit_value,
                "pnl_amount_gross": gross_pnl_amount,
                "pnl_amount_net": net_pnl_amount,
                "capital_pnl_pct_gross": capital_pnl_gross,
                "capital_pnl_pct": capital_pnl_net,
                "mfe_pct": mfe_pct,
                "mae_pct": mae_pct,
                "bars_held": bars_held,
                "stop_loss_pct": stop_loss_pct,
                "exit_stop_price": stop_price,
                "high_water_premium": high_water_premium,
                "target_pct": target_pct,
                "trailing_enabled": trailing_enabled,
                "trailing_activation_pct": trailing_activation_pct,
                "trailing_offset_pct": trailing_offset_pct,
                "trailing_lock_breakeven": trailing_lock_breakeven,
                "trailing_active": trailing_active,
                "orb_trail_activation_mfe": orb_trail_activation_mfe,
                "orb_trail_offset_pct": orb_trail_offset_pct,
                "orb_trail_min_lock_pct": orb_trail_min_lock_pct,
                "orb_trail_priority_over_regime": orb_trail_priority_over_regime,
                "orb_trail_regime_filter": orb_trail_regime_filter,
                "orb_trail_active": orb_trail_active,
                "orb_trail_stop_price": orb_trail_stop_price,
                "oi_trail_activation_mfe": oi_trail_activation_mfe,
                "oi_trail_offset_pct": oi_trail_offset_pct,
                "oi_trail_min_lock_pct": oi_trail_min_lock_pct,
                "oi_trail_priority_over_regime": oi_trail_priority_over_regime,
                "oi_trail_regime_filter": oi_trail_regime_filter,
                "oi_trail_active": oi_trail_active,
                "oi_trail_stop_price": oi_trail_stop_price,
                **cost_breakdown,
            }
        )
        self.trades.append(row)


def _load_snapshots(store: ParquetStore, start_date: str, end_date: str) -> pd.DataFrame:
    df = store.snapshots_for_date_range(start_date, end_date)
    if df.empty:
        return df
    return df.loc[:, ["trade_date", "timestamp", "snapshot_raw_json"]].copy()


def _run_scenario(
    *,
    scenario: Scenario,
    snapshots: pd.DataFrame,
    capital_allocated: float,
    cost_model: Optional[TradingCostModel] = None,
) -> pd.DataFrame:
    logger = MemorySignalLogger(capital_allocated=capital_allocated, cost_model=cost_model)
    engine = DeterministicRuleEngine(signal_logger=logger)
    engine.set_run_context(f"offline-{scenario.name}", {"risk_config": scenario.risk_config})

    current_day: Optional[str] = None
    for row in snapshots.itertuples(index=False):
        trade_day = str(row.trade_date)
        if trade_day != current_day:
            if current_day is not None:
                engine.on_session_end(date.fromisoformat(current_day))
            engine.on_session_start(date.fromisoformat(trade_day))
            current_day = trade_day
        payload = json.loads(str(row.snapshot_raw_json))
        engine.evaluate(payload)
    if current_day is not None:
        engine.on_session_end(date.fromisoformat(current_day))

    df = pd.DataFrame(logger.trades)
    if df.empty:
        return df
    df["scenario"] = scenario.name
    df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
    df["exit_time"] = pd.to_datetime(df["exit_time"], errors="coerce")
    return df.sort_values(["exit_time", "position_id"], kind="stable").reset_index(drop=True)


def _equity_from_returns(returns: list[float], initial_capital: float) -> tuple[float, float]:
    equity = float(initial_capital)
    peak = float(initial_capital)
    max_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + float(value)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, (equity / peak) - 1.0)
    return equity, max_drawdown


def _summary(df: pd.DataFrame, *, capital_allocated: float) -> dict[str, Any]:
    if df.empty:
        return {
            "trades": 0,
            "win_rate": None,
            "avg_trade_pnl_pct": None,
            "profit_factor": None,
            "end_equity": capital_allocated,
            "net_capital_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
        }
    trade_pnl_col = "pnl_pct_net" if "pnl_pct_net" in df.columns else "pnl_pct"
    pnl = df[trade_pnl_col].astype(float)
    gross_trade_pnl = df["pnl_pct"].astype(float)
    cap = df["capital_pnl_pct"].astype(float)
    winners = pnl[pnl > 0]
    losers = pnl[pnl < 0]
    gross_profit = winners.sum()
    gross_loss = abs(losers.sum())
    end_equity, max_drawdown = _equity_from_returns(cap.tolist(), capital_allocated)
    stop_exits = int((df["exit_reason"] == "STOP_LOSS").sum())
    trailing_exits = int((df["exit_reason"] == "TRAILING_STOP").sum())
    regime_shift_exits = int((df["exit_reason"] == "REGIME_SHIFT").sum())
    return {
        "trades": int(len(df)),
        "win_rate": _safe_ratio(int((pnl > 0).sum()), int(len(df))),
        "avg_trade_pnl_pct": float(pnl.mean()),
        "avg_trade_pnl_pct_gross": float(gross_trade_pnl.mean()),
        "median_trade_pnl_pct": float(pnl.median()),
        "avg_capital_pnl_pct": float(cap.mean()),
        "profit_factor": (_safe_ratio(gross_profit, gross_loss) if gross_loss > 0 else None),
        "avg_mfe_pct": float(df["mfe_pct"].astype(float).mean()),
        "avg_mae_pct": float(df["mae_pct"].astype(float).mean()),
        "avg_bars_held": float(df["bars_held"].astype(float).mean()),
        "avg_lots": float(df["lots"].astype(float).mean()),
        "avg_cost_amount": float(df["total_cost_amount"].astype(float).mean()) if "total_cost_amount" in df.columns else 0.0,
        "total_cost_amount": float(df["total_cost_amount"].astype(float).sum()) if "total_cost_amount" in df.columns else 0.0,
        "end_equity": end_equity,
        "net_capital_return_pct": _safe_ratio(end_equity - capital_allocated, capital_allocated),
        "max_drawdown_pct": max_drawdown,
        "stop_loss_exit_pct": _safe_ratio(stop_exits, int(len(df))),
        "trailing_stop_exit_pct": _safe_ratio(trailing_exits, int(len(df))),
        "regime_shift_exit_pct": _safe_ratio(regime_shift_exits, int(len(df))),
    }


def _group_table(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for keys, group in df.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        trade_pnl_col = "pnl_pct_net" if "pnl_pct_net" in group.columns else "pnl_pct"
        pnl = group[trade_pnl_col].astype(float)
        cap = group["capital_pnl_pct"].astype(float)
        winners = pnl[pnl > 0]
        losers = pnl[pnl < 0]
        gross_loss = abs(losers.sum())
        row = {col: value for col, value in zip(group_cols, keys)}
        row.update(
            {
                "trades": int(len(group)),
                "win_rate": _safe_ratio(int((pnl > 0).sum()), int(len(group))),
                "avg_trade_pnl_pct": float(pnl.mean()),
                "median_trade_pnl_pct": float(pnl.median()),
                "avg_capital_pnl_pct": float(cap.mean()),
                "total_capital_pnl_pct": float(cap.sum()),
                "profit_factor": (_safe_ratio(winners.sum(), gross_loss) if gross_loss > 0 else None),
                "avg_mfe_pct": float(group["mfe_pct"].astype(float).mean()),
                "avg_mae_pct": float(group["mae_pct"].astype(float).mean()),
                "avg_bars_held": float(group["bars_held"].astype(float).mean()),
                "avg_lots": float(group["lots"].astype(float).mean()),
                "avg_cost_amount": float(group["total_cost_amount"].astype(float).mean()) if "total_cost_amount" in group.columns else 0.0,
                "total_cost_amount": float(group["total_cost_amount"].astype(float).sum()) if "total_cost_amount" in group.columns else 0.0,
            }
        )
        rows.append(row)
    out = pd.DataFrame(rows)
    return out.sort_values(["trades", "total_capital_pnl_pct"], ascending=[False, False], kind="stable")


def _scenario_definitions() -> list[Scenario]:
    return [
        Scenario("baseline_default", {}),
        Scenario("tight_stop_10", {"stop_loss_pct": 0.10, "target_pct": 0.80, "trailing_enabled": False}),
        Scenario("medium_stop_20", {"stop_loss_pct": 0.20, "target_pct": 0.80, "trailing_enabled": False}),
        Scenario(
            "tight_stop_10_trailing",
            {
                "stop_loss_pct": 0.10,
                "target_pct": 0.80,
                "trailing_enabled": True,
                "trailing_activation_pct": 0.10,
                "trailing_offset_pct": 0.05,
                "trailing_lock_breakeven": True,
            },
        ),
        Scenario(
            "medium_stop_20_trailing",
            {
                "stop_loss_pct": 0.20,
                "target_pct": 0.80,
                "trailing_enabled": True,
                "trailing_activation_pct": 0.15,
                "trailing_offset_pct": 0.07,
                "trailing_lock_breakeven": True,
            },
        ),
    ]


def _markdown_table(df: pd.DataFrame, limit: Optional[int] = None) -> str:
    if df.empty:
        return "_No rows_"
    frame = df.head(limit) if limit else df
    display = frame.copy()
    for col in display.columns:
        if pd.api.types.is_float_dtype(display[col]):
            display[col] = display[col].map(lambda value: "" if pd.isna(value) else f"{value:.6f}")
        else:
            display[col] = display[col].map(lambda value: "" if pd.isna(value) else str(value))
    headers = [str(col) for col in display.columns]
    sep = ["---"] * len(headers)
    rows = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
    for row in display.itertuples(index=False, name=None):
        rows.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(rows)


def _write_report(
    *,
    output_dir: Path,
    start_date: str,
    end_date: str,
    scenario_summary: pd.DataFrame,
    baseline_strategy: pd.DataFrame,
    baseline_regime: pd.DataFrame,
    baseline_year: pd.DataFrame,
    baseline_strategy_regime: pd.DataFrame,
    baseline_exit: pd.DataFrame,
    notes: list[str],
) -> Path:
    report_path = output_dir / "report.md"
    lines = [
        "# Strategy Research Report",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Snapshot range: {start_date} to {end_date}",
        "",
        "## Executive Summary",
        "",
    ]
    lines.extend(f"- {note}" for note in notes)
    lines.extend(
        [
            "",
            "## Scenario Comparison",
            "",
            _markdown_table(scenario_summary),
            "",
            "## Baseline By Strategy",
            "",
            _markdown_table(baseline_strategy, limit=20),
            "",
            "## Baseline By Regime",
            "",
            _markdown_table(baseline_regime, limit=20),
            "",
            "## Baseline By Year",
            "",
            _markdown_table(baseline_year, limit=20),
            "",
            "## Baseline Strategy x Regime",
            "",
            _markdown_table(baseline_strategy_regime, limit=30),
            "",
            "## Baseline Exit Reasons",
            "",
            _markdown_table(baseline_exit, limit=20),
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Offline strategy research over snapshot parquet.")
    parser.add_argument("--parquet-base", default=str(DEFAULT_PARQUET_BASE))
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--brokerage-per-order", type=float, default=DEFAULT_BROKERAGE_PER_ORDER)
    parser.add_argument("--charges-bps-per-side", type=float, default=DEFAULT_CHARGES_BPS_PER_SIDE)
    parser.add_argument("--slippage-bps-per-side", type=float, default=DEFAULT_SLIPPAGE_BPS_PER_SIDE)
    parser.add_argument("--window-manifest", default=None, help="Path to canonical window manifest JSON.")
    parser.add_argument("--formal-run", action="store_true", help="Enforce formal readiness rules from window manifest.")
    parser.add_argument("--manifest-min-trading-days", type=int, default=DEFAULT_MIN_TRADING_DAYS)
    parser.add_argument("--manifest-required-schema-version", default=DEFAULT_REQUIRED_SCHEMA_VERSION)
    args = parser.parse_args(argv)

    os.environ["STRATEGY_REDIS_PUBLISH_ENABLED"] = "0"
    if args.formal_run and not args.window_manifest:
        raise SystemExit("--formal-run requires --window-manifest")

    manifest_meta: Optional[dict[str, Any]] = None
    if args.window_manifest:
        manifest_meta = load_and_validate_window_manifest(
            args.window_manifest,
            formal_run=bool(args.formal_run),
            required_schema_version=str(args.manifest_required_schema_version),
            min_trading_days=int(args.manifest_min_trading_days),
            context="offline_strategy_analysis.window_manifest",
        )

    manifest_start = str(manifest_meta["window_start"]) if manifest_meta else None
    manifest_end = str(manifest_meta["window_end"]) if manifest_meta else None
    min_day = manifest_start or args.start_date
    max_day = manifest_end or args.end_date
    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_CANONICAL,
        context="offline_strategy_analysis",
        parquet_base=Path(args.parquet_base),
        min_day=min_day,
        max_day=max_day,
    )
    store = ParquetStore(args.parquet_base, snapshots_dataset=SNAPSHOT_DATASET_CANONICAL)
    try:
        days = store.available_snapshot_days(min_day, max_day)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))
    if not days:
        raise SystemExit("no snapshot days available for requested range")
    start_date = str(manifest_start or args.start_date or days[0])
    end_date = str(manifest_end or args.end_date or days[-1])
    try:
        snapshots = _load_snapshots(store, start_date, end_date)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))
    if snapshots.empty:
        raise SystemExit("no snapshots loaded")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    cost_model = TradingCostModel(
        brokerage_per_order=float(args.brokerage_per_order),
        charges_bps_per_side=float(args.charges_bps_per_side),
        slippage_bps_per_side=float(args.slippage_bps_per_side),
    )

    scenario_frames: list[pd.DataFrame] = []
    scenario_rows: list[dict[str, Any]] = []
    for scenario in _scenario_definitions():
        df = _run_scenario(
            scenario=scenario,
            snapshots=snapshots,
            capital_allocated=float(args.capital),
            cost_model=cost_model,
        )
        scenario_frames.append(df)
        summary = _summary(df, capital_allocated=float(args.capital))
        summary["scenario"] = scenario.name
        scenario_rows.append(summary)
        df.to_csv(output_dir / f"trades_{scenario.name}.csv", index=False)

    scenario_summary = pd.DataFrame(scenario_rows).sort_values(
        ["net_capital_return_pct", "win_rate"], ascending=[False, False], kind="stable"
    )
    baseline_name = "baseline_default"
    baseline_df = next(df for df in scenario_frames if not df.empty and str(df["scenario"].iloc[0]) == baseline_name)
    baseline_strategy = _group_table(baseline_df, ["entry_strategy"])
    baseline_regime = _group_table(baseline_df, ["regime"])
    baseline_year = _group_table(baseline_df, ["year"])
    baseline_strategy_regime = _group_table(baseline_df, ["entry_strategy", "regime"])
    baseline_exit = _group_table(baseline_df, ["exit_reason"])

    scenario_summary.to_csv(output_dir / "scenario_summary.csv", index=False)
    baseline_strategy.to_csv(output_dir / "baseline_by_strategy.csv", index=False)
    baseline_regime.to_csv(output_dir / "baseline_by_regime.csv", index=False)
    baseline_year.to_csv(output_dir / "baseline_by_year.csv", index=False)
    baseline_strategy_regime.to_csv(output_dir / "baseline_by_strategy_regime.csv", index=False)
    baseline_exit.to_csv(output_dir / "baseline_by_exit_reason.csv", index=False)

    best = scenario_summary.iloc[0].to_dict() if len(scenario_summary) else {}
    worst = scenario_summary.iloc[-1].to_dict() if len(scenario_summary) else {}
    top_strategy = baseline_strategy.iloc[0].to_dict() if len(baseline_strategy) else {}
    worst_strategy = baseline_strategy.sort_values("total_capital_pnl_pct", ascending=True, kind="stable").iloc[0].to_dict() if len(baseline_strategy) else {}
    report = _write_report(
        output_dir=output_dir,
        start_date=start_date,
        end_date=end_date,
        scenario_summary=scenario_summary,
        baseline_strategy=baseline_strategy,
        baseline_regime=baseline_regime,
        baseline_year=baseline_year,
        baseline_strategy_regime=baseline_strategy_regime,
        baseline_exit=baseline_exit,
        notes=[
            f"Best tested risk scenario: {best.get('scenario')} with net capital return {best.get('net_capital_return_pct'):.2%} and max drawdown {best.get('max_drawdown_pct'):.2%}." if best else "No best scenario available.",
            f"Worst tested risk scenario: {worst.get('scenario')} with net capital return {worst.get('net_capital_return_pct'):.2%}." if worst else "No worst scenario available.",
            f"Top baseline strategy by total capital contribution: {top_strategy.get('entry_strategy')} ({top_strategy.get('total_capital_pnl_pct'):.2%})." if top_strategy else "No top strategy available.",
            f"Worst baseline strategy by total capital contribution: {worst_strategy.get('entry_strategy')} ({worst_strategy.get('total_capital_pnl_pct'):.2%})." if worst_strategy else "No worst strategy available.",
            "This report uses capital-weighted returns based on current lot sizing, not the dashboard shortcut that compounds raw option PnL percentages as portfolio equity.",
            f"Research costs applied: brokerage/order={cost_model.brokerage_per_order:.2f}, charges_bps/side={cost_model.charges_bps_per_side:.2f}, slippage_bps/side={cost_model.slippage_bps_per_side:.2f}.",
            "Legacy ML entry overlay removed. This report evaluates deterministic strategy behavior only.",
        ],
    )

    meta = {
        "output_dir": str(output_dir),
        "report": str(report),
        "days": int(len(days)),
        "start_date": start_date,
        "end_date": end_date,
        "rows": int(len(snapshots)),
        "cost_model": cost_model.to_metadata(),
        **snapshot_access.to_metadata(),
    }
    (output_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    split_meta = None
    try:
        split_meta = split_boundaries_for_days(days)
    except Exception:
        split_meta = None
    run_meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "command": "strategy_app.tools.offline_strategy_analysis",
        "formal_run": bool(args.formal_run),
        "exploratory_only": bool((manifest_meta or {}).get("exploratory_only", not bool(args.formal_run))),
        "window_manifest": manifest_meta,
        "manifest_hash": (manifest_meta or {}).get("manifest_hash"),
        "manifest_path": (manifest_meta or {}).get("manifest_path"),
        "window_start": start_date,
        "window_end": end_date,
        **snapshot_access.to_metadata(),
        "split_boundaries": split_meta,
        "gate_results": {
            "formal_ready": (manifest_meta or {}).get("formal_ready"),
            "required_schema_version": str(args.manifest_required_schema_version),
            "min_trading_days_required": int(args.manifest_min_trading_days),
            "window_trading_days": (manifest_meta or {}).get("trading_days"),
            "all_days_required_schema": (manifest_meta or {}).get("all_days_required_schema"),
        },
    }
    (output_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
