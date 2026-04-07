"""Run a deterministic profile tournament over historical snapshot parquet."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from snapshot_app.historical.parquet_store import ParquetStore
from snapshot_app.historical.snapshot_access import (
    DEFAULT_HISTORICAL_PARQUET_BASE,
    SNAPSHOT_DATASET_CANONICAL,
    SNAPSHOT_INPUT_MODE_CANONICAL,
    require_snapshot_access,
)
from strategy_app.engines.profiles import (
    PROFILE_DET_CORE_V2,
    PROFILE_DET_PROD_V1,
    PROFILE_DET_SETUP_V1,
    build_run_metadata,
)
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.tools.offline_strategy_analysis import (
    DEFAULT_BROKERAGE_PER_ORDER,
    DEFAULT_CHARGES_BPS_PER_SIDE,
    DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    MemorySignalLogger,
    TradingCostModel,
    _group_table,
    _summary,
)

DEFAULT_PARQUET_BASE = DEFAULT_HISTORICAL_PARQUET_BASE
DEFAULT_OUTPUT_ROOT = Path(".run/deterministic_profile_tournament")
DEFAULT_CAPITAL = 500000.0


@dataclass(frozen=True)
class TournamentWindow:
    label: str
    date_from: str
    date_to: str


@dataclass(frozen=True)
class DeterministicProfileSpec:
    profile_id: str
    label: str
    description: str
    metadata: dict[str, Any]


def _router_config(
    *,
    profile_id: str,
    regime_entry_map: dict[str, list[str]],
    exit_strategies: list[str],
) -> dict[str, Any]:
    return {
        "strategy_profile_id": profile_id,
        "regime_entry_map": {str(key): [str(item).upper() for item in value] for key, value in regime_entry_map.items()},
        "exit_strategies": [str(item).upper() for item in exit_strategies],
    }


def default_profile_specs() -> list[DeterministicProfileSpec]:
    return [
        DeterministicProfileSpec(
            profile_id=PROFILE_DET_CORE_V2,
            label="Core V2 Baseline",
            description="Current internal comparison baseline.",
            metadata=build_run_metadata(PROFILE_DET_CORE_V2),
        ),
        DeterministicProfileSpec(
            profile_id="det_orb_only_v1",
            label="ORB Only",
            description="ORB-led directional profile with high-vol ORB preserved and ORB-only exits.",
            metadata={
                "strategy_profile_id": "det_orb_only_v1",
                "router_config": _router_config(
                    profile_id="det_orb_only_v1",
                    regime_entry_map={
                        "TRENDING": ["IV_FILTER", "ORB"],
                        "SIDEWAYS": ["IV_FILTER"],
                        "EXPIRY": ["IV_FILTER"],
                        "PRE_EXPIRY": ["IV_FILTER", "ORB"],
                        "HIGH_VOL": ["IV_FILTER", "HIGH_VOL_ORB"],
                        "AVOID": [],
                    },
                    exit_strategies=["ORB", "HIGH_VOL_ORB"],
                ),
            },
        ),
        DeterministicProfileSpec(
            profile_id="det_oi_only_v1",
            label="OI Only",
            description="OI_BUILDUP-only directional profile focused on cleaner trend participation.",
            metadata={
                "strategy_profile_id": "det_oi_only_v1",
                "router_config": _router_config(
                    profile_id="det_oi_only_v1",
                    regime_entry_map={
                        "TRENDING": ["IV_FILTER", "OI_BUILDUP"],
                        "SIDEWAYS": ["IV_FILTER", "OI_BUILDUP"],
                        "EXPIRY": ["IV_FILTER"],
                        "PRE_EXPIRY": ["IV_FILTER", "OI_BUILDUP"],
                        "HIGH_VOL": ["IV_FILTER"],
                        "AVOID": [],
                    },
                    exit_strategies=["OI_BUILDUP"],
                ),
            },
        ),
        DeterministicProfileSpec(
            profile_id="det_orb_oi_combo_v1",
            label="ORB + OI Combo",
            description="Production-style ORB + OI combination without EMA/VWAP noise.",
            metadata={
                "strategy_profile_id": "det_orb_oi_combo_v1",
                "router_config": _router_config(
                    profile_id="det_orb_oi_combo_v1",
                    regime_entry_map={
                        "TRENDING": ["IV_FILTER", "ORB", "OI_BUILDUP"],
                        "SIDEWAYS": ["IV_FILTER", "OI_BUILDUP"],
                        "EXPIRY": ["IV_FILTER"],
                        "PRE_EXPIRY": ["IV_FILTER", "ORB", "OI_BUILDUP"],
                        "HIGH_VOL": ["IV_FILTER", "HIGH_VOL_ORB"],
                        "AVOID": [],
                    },
                    exit_strategies=["ORB", "OI_BUILDUP", "HIGH_VOL_ORB"],
                ),
            },
        ),
        DeterministicProfileSpec(
            profile_id=PROFILE_DET_PROD_V1,
            label="ORB + OI Safe (Production)",
            description="Promoted production baseline sourced from the shared profile registry.",
            metadata=build_run_metadata(PROFILE_DET_PROD_V1),
        ),
        DeterministicProfileSpec(
            profile_id=PROFILE_DET_SETUP_V1,
            label="Trader Setup v1",
            description="Experimental trader-style setup profile using retests, pullbacks, and failed-break reversals.",
            metadata=build_run_metadata(PROFILE_DET_SETUP_V1),
        ),
        DeterministicProfileSpec(
            profile_id="det_ema_legacy_v1",
            label="EMA Legacy Benchmark",
            description="Legacy EMA benchmark retained only as a comparison profile.",
            metadata={
                "strategy_profile_id": "det_ema_legacy_v1",
                "router_config": _router_config(
                    profile_id="det_ema_legacy_v1",
                    regime_entry_map={
                        "TRENDING": ["EMA_CROSSOVER"],
                        "SIDEWAYS": [],
                        "EXPIRY": [],
                        "PRE_EXPIRY": ["EMA_CROSSOVER"],
                        "HIGH_VOL": [],
                        "AVOID": [],
                    },
                    exit_strategies=["EMA_CROSSOVER"],
                ),
            },
        ),
    ]


def _load_profile_specs(path: Optional[str]) -> list[DeterministicProfileSpec]:
    if not path:
        return default_profile_specs()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("profile spec file must be a JSON list")
    out: list[DeterministicProfileSpec] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        profile_id = str(item.get("profile_id") or "").strip()
        if not profile_id:
            continue
        out.append(
            DeterministicProfileSpec(
                profile_id=profile_id,
                label=str(item.get("label") or profile_id).strip() or profile_id,
                description=str(item.get("description") or "").strip(),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    if not out:
        raise ValueError("no valid profiles found in profile spec file")
    return out


def build_calendar_windows(trade_dates: list[str], mode: str) -> list[TournamentWindow]:
    ordered = sorted({str(day).strip() for day in trade_dates if str(day).strip()})
    if not ordered:
        return []
    mode_text = str(mode or "full").strip().lower()
    if mode_text == "full":
        return [TournamentWindow(label=f"{ordered[0]}_to_{ordered[-1]}", date_from=ordered[0], date_to=ordered[-1])]

    grouped: dict[str, list[str]] = {}
    for day in ordered:
        ts = pd.Timestamp(day)
        if mode_text == "monthly":
            key = f"{ts.year:04d}-{ts.month:02d}"
        elif mode_text == "quarterly":
            quarter = ((int(ts.month) - 1) // 3) + 1
            key = f"{ts.year:04d}-Q{quarter}"
        elif mode_text == "yearly":
            key = f"{ts.year:04d}"
        else:
            raise ValueError(f"unsupported window mode '{mode}'")
        grouped.setdefault(key, []).append(day)

    return [
        TournamentWindow(label=label, date_from=min(days), date_to=max(days))
        for label, days in sorted(grouped.items(), key=lambda item: item[0])
    ]


def _load_snapshots(store: ParquetStore, *, date_from: str, date_to: str) -> pd.DataFrame:
    frame = store.snapshots_for_date_range(date_from, date_to)
    if frame.empty:
        return frame
    return frame.loc[:, ["trade_date", "timestamp", "snapshot_raw_json"]].copy()


def _run_profile_window(
    *,
    snapshots: pd.DataFrame,
    profile: DeterministicProfileSpec,
    capital_allocated: float,
    cost_model: Optional[TradingCostModel] = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    logger = MemorySignalLogger(capital_allocated=capital_allocated, cost_model=cost_model)
    engine = DeterministicRuleEngine(signal_logger=logger, strategy_profile_id=profile.profile_id)
    engine.set_run_context(f"tournament-{profile.profile_id}", profile.metadata)

    current_day: Optional[str] = None
    for row in snapshots.itertuples(index=False):
        trade_day = str(row.trade_date)
        if trade_day != current_day:
            if current_day is not None:
                engine.on_session_end(pd.Timestamp(current_day).date())
            engine.on_session_start(pd.Timestamp(trade_day).date())
            current_day = trade_day
        payload = json.loads(str(row.snapshot_raw_json))
        engine.evaluate(payload)
    if current_day is not None:
        engine.on_session_end(pd.Timestamp(current_day).date())

    trades = pd.DataFrame(logger.trades)
    if not trades.empty:
        trades["entry_time"] = pd.to_datetime(trades["entry_time"], errors="coerce")
        trades["exit_time"] = pd.to_datetime(trades["exit_time"], errors="coerce")
        trades = trades.sort_values(["exit_time", "position_id"], kind="stable").reset_index(drop=True)
    summary = _summary(trades, capital_allocated=float(capital_allocated))
    return trades, summary


def _window_result_rows(
    *,
    window: TournamentWindow,
    profiles: list[DeterministicProfileSpec],
    store: ParquetStore,
    capital_allocated: float,
    cost_model: Optional[TradingCostModel] = None,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    snapshots = _load_snapshots(store, date_from=window.date_from, date_to=window.date_to)
    rows: list[dict[str, Any]] = []
    trade_frames: dict[str, pd.DataFrame] = {}
    for profile in profiles:
        trades, summary = _run_profile_window(
            snapshots=snapshots,
            profile=profile,
            capital_allocated=float(capital_allocated),
            cost_model=cost_model,
        )
        row = {
            "window_label": window.label,
            "date_from": window.date_from,
            "date_to": window.date_to,
            "profile_id": profile.profile_id,
            "profile_label": profile.label,
            **summary,
        }
        rows.append(row)
        trade_frames[profile.profile_id] = trades
    window_df = pd.DataFrame(rows)
    if window_df.empty:
        return window_df, trade_frames
    baseline_row = window_df[window_df["profile_id"] == PROFILE_DET_CORE_V2]
    baseline_return = float(baseline_row.iloc[0]["net_capital_return_pct"]) if not baseline_row.empty else 0.0
    baseline_dd = float(baseline_row.iloc[0]["max_drawdown_pct"]) if not baseline_row.empty else 0.0
    window_df["vs_baseline_return_pct"] = window_df["net_capital_return_pct"].astype(float) - baseline_return
    window_df["vs_baseline_drawdown_pct"] = window_df["max_drawdown_pct"].astype(float) - baseline_dd
    window_df["profitable_window"] = window_df["net_capital_return_pct"].astype(float) > 0.0
    window_df["beat_baseline"] = window_df["vs_baseline_return_pct"].astype(float) > 0.0
    return window_df.sort_values(
        ["window_label", "net_capital_return_pct", "profit_factor"],
        ascending=[True, False, False],
        kind="stable",
    ).reset_index(drop=True), trade_frames


def aggregate_profile_results(window_results: pd.DataFrame) -> pd.DataFrame:
    if window_results.empty:
        return pd.DataFrame()
    grouped = (
        window_results.groupby(["profile_id", "profile_label"], dropna=False)
        .agg(
            windows=("window_label", "nunique"),
            total_trades=("trades", "sum"),
            avg_return_pct=("net_capital_return_pct", "mean"),
            median_return_pct=("net_capital_return_pct", "median"),
            profitable_window_pct=("profitable_window", "mean"),
            beat_baseline_pct=("beat_baseline", "mean"),
            avg_profit_factor=("profit_factor", "mean"),
            avg_win_rate=("win_rate", "mean"),
            avg_drawdown_pct=("max_drawdown_pct", "mean"),
            worst_window_return_pct=("net_capital_return_pct", "min"),
            worst_drawdown_pct=("max_drawdown_pct", "min"),
        )
        .reset_index()
    )
    return grouped.sort_values(
        [
            "profitable_window_pct",
            "beat_baseline_pct",
            "median_return_pct",
            "avg_return_pct",
            "avg_profit_factor",
            "avg_drawdown_pct",
            "total_trades",
        ],
        ascending=[False, False, False, False, False, False, False],
        kind="stable",
    ).reset_index(drop=True)


def build_recommendation(leaderboard: pd.DataFrame) -> dict[str, Any]:
    if leaderboard.empty:
        return {"status": "no_profiles", "message": "No profile results available."}
    winner = leaderboard.iloc[0].to_dict()
    return {
        "status": "ok",
        "recommended_profile_id": winner.get("profile_id"),
        "recommended_label": winner.get("profile_label"),
        "reason": (
            f"Top profile by stability-first sorting: profitable_window_pct={winner.get('profitable_window_pct'):.2%}, "
            f"beat_baseline_pct={winner.get('beat_baseline_pct'):.2%}, median_return_pct={winner.get('median_return_pct'):.2%}."
        ),
        "metrics": {
            "windows": int(winner.get("windows") or 0),
            "total_trades": int(winner.get("total_trades") or 0),
            "avg_return_pct": float(winner.get("avg_return_pct") or 0.0),
            "median_return_pct": float(winner.get("median_return_pct") or 0.0),
            "avg_drawdown_pct": float(winner.get("avg_drawdown_pct") or 0.0),
            "worst_drawdown_pct": float(winner.get("worst_drawdown_pct") or 0.0),
        },
    }


def _markdown_table(df: pd.DataFrame, limit: Optional[int] = None) -> str:
    if df.empty:
        return "_No rows_"
    frame = df.head(limit) if limit else df
    rendered = frame.copy()
    for col in rendered.columns:
        if pd.api.types.is_float_dtype(rendered[col]):
            rendered[col] = rendered[col].map(lambda value: "" if pd.isna(value) else f"{float(value):.6f}")
        else:
            rendered[col] = rendered[col].map(lambda value: "" if pd.isna(value) else str(value))
    headers = [str(col) for col in rendered.columns]
    rows = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rendered.itertuples(index=False, name=None):
        rows.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(rows)


def _write_report(
    *,
    output_dir: Path,
    date_from: str,
    date_to: str,
    window_mode: str,
    cost_model: Optional[TradingCostModel],
    leaderboard: pd.DataFrame,
    window_results: pd.DataFrame,
    recommendation: dict[str, Any],
) -> Path:
    report_path = output_dir / "tournament_report.md"
    lines = [
        "# Deterministic Profile Tournament",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Range: {date_from} to {date_to}",
        f"- Window mode: {window_mode}",
        (
            f"- Costs: brokerage/order={cost_model.brokerage_per_order:.2f}, charges_bps/side={cost_model.charges_bps_per_side:.2f}, "
            f"slippage_bps/side={cost_model.slippage_bps_per_side:.2f}"
            if cost_model is not None
            else "- Costs: default gross-only assumptions"
        ),
        "",
        "## Recommendation",
        "",
        f"- Status: {recommendation.get('status')}",
        f"- Recommended profile: {recommendation.get('recommended_profile_id') or '--'}",
        f"- Reason: {recommendation.get('reason') or recommendation.get('message') or '--'}",
        "",
        "## Leaderboard",
        "",
        _markdown_table(leaderboard, limit=20),
        "",
        "## Per-Window Results",
        "",
        _markdown_table(window_results, limit=50),
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _write_profile_manifest(output_dir: Path, profiles: list[DeterministicProfileSpec]) -> None:
    payload = [asdict(item) for item in profiles]
    (output_dir / "profiles.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_tournament(
    *,
    parquet_base: str,
    date_from: str,
    date_to: str,
    capital: float,
    window_mode: str,
    output_dir: Path,
    profile_spec_path: Optional[str] = None,
    save_top_trade_exports: int = 3,
    cost_model: Optional[TradingCostModel] = None,
) -> dict[str, Any]:
    require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_CANONICAL,
        context="deterministic_profile_tournament",
        parquet_base=Path(parquet_base),
        min_day=date_from,
        max_day=date_to,
    )
    store = ParquetStore(parquet_base, snapshots_dataset=SNAPSHOT_DATASET_CANONICAL)
    available_days = store.available_snapshot_days(date_from, date_to)
    if not available_days:
        raise ValueError("no snapshot days available for requested range")
    windows = build_calendar_windows(list(available_days), window_mode)
    if not windows:
        raise ValueError("no windows derived from available trade dates")
    profiles = _load_profile_specs(profile_spec_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_profile_manifest(output_dir, profiles)

    all_window_results: list[pd.DataFrame] = []
    baseline_trade_frames: list[pd.DataFrame] = []
    saved_trade_exports = 0
    for window in windows:
        window_df, trade_frames = _window_result_rows(
            window=window,
            profiles=profiles,
            store=store,
            capital_allocated=float(capital),
            cost_model=cost_model,
        )
        if window_df.empty:
            continue
        all_window_results.append(window_df)
        baseline_trades = trade_frames.get(PROFILE_DET_CORE_V2)
        if isinstance(baseline_trades, pd.DataFrame) and not baseline_trades.empty:
            baseline_trade_frames.append(baseline_trades.copy())
        safe_label = window.label.replace(":", "_").replace("/", "_")
        window_df.to_csv(output_dir / f"window_results_{safe_label}.csv", index=False)
        if saved_trade_exports < int(save_top_trade_exports):
            best_row = window_df.sort_values(
                ["net_capital_return_pct", "profit_factor"],
                ascending=[False, False],
                kind="stable",
            ).iloc[0]
            best_profile_id = str(best_row.get("profile_id") or "")
            best_trades = trade_frames.get(best_profile_id)
            if isinstance(best_trades, pd.DataFrame) and not best_trades.empty:
                best_trades.to_csv(output_dir / f"top_trades_{safe_label}_{best_profile_id}.csv", index=False)
                saved_trade_exports += 1

    if not all_window_results:
        raise ValueError("no tournament results were generated")

    window_results = pd.concat(all_window_results, ignore_index=True)
    leaderboard = aggregate_profile_results(window_results)
    recommendation = build_recommendation(leaderboard)

    window_results.to_csv(output_dir / "window_results_all.csv", index=False)
    leaderboard.to_csv(output_dir / "profile_leaderboard.csv", index=False)
    (output_dir / "recommendation.json").write_text(json.dumps(recommendation, indent=2), encoding="utf-8")
    _write_report(
        output_dir=output_dir,
        date_from=date_from,
        date_to=date_to,
        window_mode=window_mode,
        cost_model=cost_model,
        leaderboard=leaderboard,
        window_results=window_results,
        recommendation=recommendation,
    )

    non_empty_baseline_frames = [frame for frame in baseline_trade_frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if non_empty_baseline_frames:
        baseline_group = _group_table(pd.concat(non_empty_baseline_frames, ignore_index=True), ["entry_strategy"])
        baseline_group.to_csv(output_dir / "baseline_by_strategy.csv", index=False)

    return {
        "output_dir": str(output_dir),
        "windows": len(windows),
        "profiles": len(profiles),
        "cost_model": cost_model.to_metadata() if cost_model is not None else None,
        "recommended_profile_id": recommendation.get("recommended_profile_id"),
        "recommended_label": recommendation.get("recommended_label"),
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a deterministic profile tournament over historical parquet snapshots.")
    parser.add_argument("--parquet-base", default=str(DEFAULT_PARQUET_BASE))
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--window-mode", choices=["full", "monthly", "quarterly", "yearly"], default="monthly")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--profile-spec", default=None, help="Optional JSON file overriding the default profile set.")
    parser.add_argument("--save-top-trade-exports", type=int, default=3)
    parser.add_argument("--brokerage-per-order", type=float, default=DEFAULT_BROKERAGE_PER_ORDER)
    parser.add_argument("--charges-bps-per-side", type=float, default=DEFAULT_CHARGES_BPS_PER_SIDE)
    parser.add_argument("--slippage-bps-per-side", type=float, default=DEFAULT_SLIPPAGE_BPS_PER_SIDE)
    args = parser.parse_args(list(argv) if argv is not None else None)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / stamp
    cost_model = TradingCostModel(
        brokerage_per_order=float(args.brokerage_per_order),
        charges_bps_per_side=float(args.charges_bps_per_side),
        slippage_bps_per_side=float(args.slippage_bps_per_side),
    )
    result = run_tournament(
        parquet_base=str(args.parquet_base),
        date_from=str(args.date_from),
        date_to=str(args.date_to),
        capital=float(args.capital),
        window_mode=str(args.window_mode),
        output_dir=output_dir,
        profile_spec_path=(str(args.profile_spec) if args.profile_spec else None),
        save_top_trade_exports=int(args.save_top_trade_exports),
        cost_model=cost_model,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
