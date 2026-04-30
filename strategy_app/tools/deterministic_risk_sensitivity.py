"""Run stop-loss and trailing sensitivity for the winning deterministic profile."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
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
    PROFILE_DET_PROD_V1,
    build_router_config,
    get_regime_entry_map,
)
from strategy_app.tools.deterministic_profile_tournament import (
    DEFAULT_CAPITAL,
    DeterministicProfileSpec,
    TournamentWindow,
    _load_snapshots,
    _markdown_table,
    _run_profile_window,
    build_calendar_windows,
)
from strategy_app.tools.offline_strategy_analysis import _group_table
from strategy_app.tools.offline_strategy_analysis import (
    DEFAULT_BROKERAGE_PER_ORDER,
    DEFAULT_CHARGES_BPS_PER_SIDE,
    DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    TradingCostModel,
)

DEFAULT_PARQUET_BASE = DEFAULT_HISTORICAL_PARQUET_BASE
DEFAULT_OUTPUT_ROOT = Path(".run/deterministic_risk_sensitivity")
DEFAULT_BASE_PROFILE_ID = PROFILE_DET_PROD_V1


def default_winner_variants() -> list[DeterministicProfileSpec]:
    base_router = build_router_config(PROFILE_DET_PROD_V1)
    variants: list[DeterministicProfileSpec] = []
    for stop in (0.15, 0.20, 0.25, 0.30):
        stop_text = f"{int(stop * 100):02d}"
        for trailing_enabled in (False, True):
            suffix = "trail" if trailing_enabled else "hard"
            profile_id = f"det_orb_oi_safe_sl{stop_text}_{suffix}"
            label = f"Safe SL {int(stop * 100)}% {'+ Trail' if trailing_enabled else 'Only'}"
            risk_config: dict[str, Any] = {
                "stop_loss_pct": float(stop),
                "target_pct": 0.80,
                "trailing_enabled": bool(trailing_enabled),
                "trailing_activation_pct": 0.10,
                "trailing_offset_pct": 0.05,
                "trailing_lock_breakeven": True,
            }
            variants.append(
                DeterministicProfileSpec(
                    profile_id=profile_id,
                    label=label,
                    description=f"Winner profile sensitivity variant: stop={stop:.0%}, trailing={trailing_enabled}.",
                    metadata={
                        "strategy_profile_id": profile_id,
                        "router_config": {
                            "strategy_profile_id": profile_id,
                            "regime_entry_map": get_regime_entry_map(PROFILE_DET_PROD_V1),
                            "exit_strategies": list(base_router["exit_strategies"]),
                        },
                        "risk_config": risk_config,
                    },
                )
            )
    return variants


def _window_result_rows(
    *,
    window: TournamentWindow,
    profiles: list[DeterministicProfileSpec],
    store: ParquetStore,
    capital_allocated: float,
    baseline_profile_id: str,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    snapshots = _load_snapshots(store, date_from=window.date_from, date_to=window.date_to)
    rows: list[dict[str, Any]] = []
    trade_frames: dict[str, pd.DataFrame] = {}
    for profile in profiles:
        trades, summary = _run_profile_window(
            snapshots=snapshots,
            profile=profile,
            capital_allocated=float(capital_allocated),
        )
        rows.append(
            {
                "window_label": window.label,
                "date_from": window.date_from,
                "date_to": window.date_to,
                "profile_id": profile.profile_id,
                "profile_label": profile.label,
                **summary,
            }
        )
        trade_frames[profile.profile_id] = trades
    window_df = pd.DataFrame(rows)
    if window_df.empty:
        return window_df, trade_frames
    baseline_row = window_df[window_df["profile_id"] == baseline_profile_id]
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


def aggregate_variant_results(window_results: pd.DataFrame) -> pd.DataFrame:
    if window_results.empty:
        return pd.DataFrame()
    leaderboard = (
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
            avg_stop_loss_exit_pct=("stop_loss_exit_pct", "mean"),
            avg_trailing_stop_exit_pct=("trailing_stop_exit_pct", "mean"),
        )
        .reset_index()
    )
    return leaderboard.sort_values(
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
        return {"status": "no_variants", "message": "No risk variants produced results."}
    winner = leaderboard.iloc[0].to_dict()
    return {
        "status": "ok",
        "recommended_variant_id": winner.get("profile_id"),
        "recommended_label": winner.get("profile_label"),
        "reason": (
            f"Top risk variant by stability-first sorting: profitable_window_pct={winner.get('profitable_window_pct'):.2%}, "
            f"beat_baseline_pct={winner.get('beat_baseline_pct'):.2%}, median_return_pct={winner.get('median_return_pct'):.2%}."
        ),
        "metrics": {
            "windows": int(winner.get("windows") or 0),
            "total_trades": int(winner.get("total_trades") or 0),
            "avg_return_pct": float(winner.get("avg_return_pct") or 0.0),
            "median_return_pct": float(winner.get("median_return_pct") or 0.0),
            "avg_drawdown_pct": float(winner.get("avg_drawdown_pct") or 0.0),
            "worst_drawdown_pct": float(winner.get("worst_drawdown_pct") or 0.0),
            "avg_stop_loss_exit_pct": float(winner.get("avg_stop_loss_exit_pct") or 0.0),
            "avg_trailing_stop_exit_pct": float(winner.get("avg_trailing_stop_exit_pct") or 0.0),
        },
    }


def _write_variant_manifest(output_dir: Path, profiles: list[DeterministicProfileSpec]) -> None:
    (output_dir / "variants.json").write_text(json.dumps([asdict(item) for item in profiles], indent=2), encoding="utf-8")


def _write_report(
    *,
    output_dir: Path,
    date_from: str,
    date_to: str,
    window_mode: str,
    baseline_profile_id: str,
    cost_model: Optional[TradingCostModel],
    leaderboard: pd.DataFrame,
    window_results: pd.DataFrame,
    recommendation: dict[str, Any],
) -> None:
    lines = [
        "# Deterministic Risk Sensitivity",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Range: {date_from} to {date_to}",
        f"- Window mode: {window_mode}",
        f"- Baseline variant: {baseline_profile_id}",
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
        f"- Recommended variant: {recommendation.get('recommended_variant_id') or '--'}",
        f"- Reason: {recommendation.get('reason') or recommendation.get('message') or '--'}",
        "",
        "## Variant Leaderboard",
        "",
        _markdown_table(leaderboard, limit=20),
        "",
        "## Per-Window Results",
        "",
        _markdown_table(window_results, limit=60),
        "",
    ]
    (output_dir / "sensitivity_report.md").write_text("\n".join(lines), encoding="utf-8")


def run_sensitivity(
    *,
    parquet_base: str,
    date_from: str,
    date_to: str,
    capital: float,
    window_mode: str,
    output_dir: Path,
    baseline_profile_id: str = "det_orb_oi_safe_sl20_trail",
    cost_model: Optional[TradingCostModel] = None,
) -> dict[str, Any]:
    require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_CANONICAL,
        context="deterministic_risk_sensitivity",
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
    profiles = default_winner_variants()
    if baseline_profile_id not in {item.profile_id for item in profiles}:
        raise ValueError(f"baseline_profile_id '{baseline_profile_id}' is not one of the generated variants")

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_variant_manifest(output_dir, profiles)

    all_window_results: list[pd.DataFrame] = []
    baseline_trade_frames: list[pd.DataFrame] = []
    for window in windows:
        window_df, trade_frames = _window_result_rows(
            window=window,
            profiles=profiles,
            store=store,
            capital_allocated=float(capital),
            baseline_profile_id=baseline_profile_id,
            cost_model=cost_model,
        )
        if window_df.empty:
            continue
        all_window_results.append(window_df)
        baseline_trades = trade_frames.get(baseline_profile_id)
        if isinstance(baseline_trades, pd.DataFrame) and not baseline_trades.empty:
            baseline_trade_frames.append(baseline_trades.copy())
        safe_label = window.label.replace(":", "_").replace("/", "_")
        window_df.to_csv(output_dir / f"window_results_{safe_label}.csv", index=False)

    if not all_window_results:
        raise ValueError("no sensitivity results were generated")

    window_results = pd.concat(all_window_results, ignore_index=True)
    leaderboard = aggregate_variant_results(window_results)
    recommendation = build_recommendation(leaderboard)

    window_results.to_csv(output_dir / "window_results_all.csv", index=False)
    leaderboard.to_csv(output_dir / "variant_leaderboard.csv", index=False)
    (output_dir / "recommendation.json").write_text(json.dumps(recommendation, indent=2), encoding="utf-8")
    _write_report(
        output_dir=output_dir,
        date_from=date_from,
        date_to=date_to,
        window_mode=window_mode,
        baseline_profile_id=baseline_profile_id,
        cost_model=cost_model,
        leaderboard=leaderboard,
        window_results=window_results,
        recommendation=recommendation,
    )

    non_empty_baseline_frames = [frame for frame in baseline_trade_frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if non_empty_baseline_frames:
        baseline_group = _group_table(pd.concat(non_empty_baseline_frames, ignore_index=True), ["entry_strategy"])
        baseline_group.to_csv(output_dir / "baseline_variant_by_strategy.csv", index=False)

    return {
        "output_dir": str(output_dir),
        "windows": len(windows),
        "variants": len(profiles),
        "baseline_profile_id": baseline_profile_id,
        "cost_model": cost_model.to_metadata() if cost_model is not None else None,
        "recommended_variant_id": recommendation.get("recommended_variant_id"),
        "recommended_label": recommendation.get("recommended_label"),
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run stop-loss and trailing sensitivity for the winning deterministic profile.")
    parser.add_argument("--parquet-base", default=str(DEFAULT_PARQUET_BASE))
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--window-mode", choices=["full", "monthly", "quarterly", "yearly"], default="quarterly")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--baseline-profile-id", default="det_orb_oi_safe_sl20_trail")
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
    result = run_sensitivity(
        parquet_base=str(args.parquet_base),
        date_from=str(args.date_from),
        date_to=str(args.date_to),
        capital=float(args.capital),
        window_mode=str(args.window_mode),
        output_dir=output_dir,
        baseline_profile_id=str(args.baseline_profile_id),
        cost_model=cost_model,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
