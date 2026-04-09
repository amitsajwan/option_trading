"""Run a multi-scenario deterministic research suite and summarize the winners."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from snapshot_app.historical.snapshot_access import DEFAULT_HISTORICAL_PARQUET_BASE
from strategy_app.engines.profiles import (
    PROFILE_DET_CORE_V2,
    PROFILE_DET_PROD_V1,
    PROFILE_DET_SETUP_V1,
    PROFILE_DET_V3_V1,
)
from strategy_app.tools.deterministic_profile_tournament import (
    DEFAULT_CAPITAL,
    _markdown_table,
    run_tournament,
)
from strategy_app.tools.deterministic_risk_sensitivity import run_sensitivity
from strategy_app.tools.offline_strategy_analysis import (
    DEFAULT_BROKERAGE_PER_ORDER,
    DEFAULT_CHARGES_BPS_PER_SIDE,
    DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    TradingCostModel,
)

DEFAULT_PARQUET_BASE = DEFAULT_HISTORICAL_PARQUET_BASE
DEFAULT_OUTPUT_ROOT = Path(".run/deterministic_research_suite")
DEFAULT_EXPORT_PROFILE_TRADE_IDS = [
    PROFILE_DET_CORE_V2,
    PROFILE_DET_PROD_V1,
    PROFILE_DET_SETUP_V1,
    PROFILE_DET_V3_V1,
]


@dataclass(frozen=True)
class ResearchScenario:
    name: str
    kind: str
    date_from: str
    date_to: str
    window_mode: str
    description: str


def default_suite_scenarios(*, date_from: str, date_to: str, anchor_date_from: Optional[str] = None) -> list[ResearchScenario]:
    anchor_from = str(anchor_date_from or date_from).strip() or str(date_from)
    return [
        ResearchScenario(
            name="primary_monthly",
            kind="tournament",
            date_from=str(date_from),
            date_to=str(date_to),
            window_mode="monthly",
            description="Recent range split monthly to expose short-horizon instability.",
        ),
        ResearchScenario(
            name="primary_quarterly",
            kind="tournament",
            date_from=str(date_from),
            date_to=str(date_to),
            window_mode="quarterly",
            description="Recent range split quarterly for headline comparison.",
        ),
        ResearchScenario(
            name="primary_full",
            kind="tournament",
            date_from=str(date_from),
            date_to=str(date_to),
            window_mode="full",
            description="Recent range as one compounded window.",
        ),
        ResearchScenario(
            name="anchor_quarterly",
            kind="tournament",
            date_from=anchor_from,
            date_to=str(date_to),
            window_mode="quarterly",
            description="Longer anchor range split quarterly for stability testing.",
        ),
        ResearchScenario(
            name="anchor_full",
            kind="tournament",
            date_from=anchor_from,
            date_to=str(date_to),
            window_mode="full",
            description="Longer anchor range as one compounded window.",
        ),
    ]


def aggregate_suite_profile_results(scenario_leaderboards: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for scenario_name, leaderboard in scenario_leaderboards.items():
        if leaderboard.empty:
            continue
        ordered = leaderboard.reset_index(drop=True).copy()
        ordered["scenario_name"] = scenario_name
        ordered["scenario_rank"] = ordered.index + 1
        rows.extend(ordered.to_dict(orient="records"))
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    grouped = (
        frame.groupby(["profile_id", "profile_label"], dropna=False)
        .agg(
            scenarios=("scenario_name", "nunique"),
            scenario_wins=("scenario_rank", lambda values: int(sum(1 for item in values if int(item) == 1))),
            scenario_podiums=("scenario_rank", lambda values: int(sum(1 for item in values if int(item) <= 3))),
            avg_rank=("scenario_rank", "mean"),
            best_rank=("scenario_rank", "min"),
            worst_rank=("scenario_rank", "max"),
            avg_return_pct=("avg_return_pct", "mean"),
            median_return_pct=("median_return_pct", "mean"),
            avg_profitable_window_pct=("profitable_window_pct", "mean"),
            avg_beat_baseline_pct=("beat_baseline_pct", "mean"),
            avg_profit_factor=("avg_profit_factor", "mean"),
            avg_win_rate=("avg_win_rate", "mean"),
            avg_drawdown_pct=("avg_drawdown_pct", "mean"),
            worst_window_return_pct=("worst_window_return_pct", "min"),
            worst_drawdown_pct=("worst_drawdown_pct", "min"),
            avg_total_trades=("total_trades", "mean"),
        )
        .reset_index()
    )
    return grouped.sort_values(
        [
            "scenario_wins",
            "scenario_podiums",
            "avg_rank",
            "avg_profitable_window_pct",
            "avg_beat_baseline_pct",
            "avg_return_pct",
            "avg_profit_factor",
        ],
        ascending=[False, False, True, False, False, False, False],
        kind="stable",
    ).reset_index(drop=True)


def build_suite_recommendation(profile_leaderboard: pd.DataFrame, scenario_recommendations: pd.DataFrame) -> dict[str, Any]:
    if profile_leaderboard.empty:
        return {"status": "no_profiles", "message": "No scenario outputs were generated."}
    winner = profile_leaderboard.iloc[0].to_dict()
    wins = scenario_recommendations[
        scenario_recommendations["recommended_profile_id"].astype(str) == str(winner.get("profile_id") or "")
    ]
    return {
        "status": "ok",
        "recommended_profile_id": winner.get("profile_id"),
        "recommended_label": winner.get("profile_label"),
        "reason": (
            f"Top profile across scenarios: wins={int(winner.get('scenario_wins') or 0)}, "
            f"podiums={int(winner.get('scenario_podiums') or 0)}, avg_rank={float(winner.get('avg_rank') or 0.0):.2f}."
        ),
        "metrics": {
            "scenarios": int(winner.get("scenarios") or 0),
            "scenario_wins": int(winner.get("scenario_wins") or 0),
            "scenario_podiums": int(winner.get("scenario_podiums") or 0),
            "avg_rank": float(winner.get("avg_rank") or 0.0),
            "avg_return_pct": float(winner.get("avg_return_pct") or 0.0),
            "avg_profitable_window_pct": float(winner.get("avg_profitable_window_pct") or 0.0),
            "avg_beat_baseline_pct": float(winner.get("avg_beat_baseline_pct") or 0.0),
        },
        "winning_scenarios": wins["scenario_name"].astype(str).tolist(),
    }


def _write_suite_report(
    *,
    output_dir: Path,
    scenarios: list[ResearchScenario],
    scenario_recommendations: pd.DataFrame,
    profile_leaderboard: pd.DataFrame,
    suite_recommendation: dict[str, Any],
    follow_up: dict[str, Any],
    cost_model: TradingCostModel,
) -> None:
    scenario_manifest = pd.DataFrame([asdict(item) for item in scenarios])
    lines = [
        "# Deterministic Research Suite",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        (
            f"- Costs: brokerage/order={cost_model.brokerage_per_order:.2f}, "
            f"charges_bps/side={cost_model.charges_bps_per_side:.2f}, "
            f"slippage_bps/side={cost_model.slippage_bps_per_side:.2f}"
        ),
        "",
        "## Suite Recommendation",
        "",
        f"- Status: {suite_recommendation.get('status')}",
        f"- Recommended profile: {suite_recommendation.get('recommended_profile_id') or '--'}",
        f"- Reason: {suite_recommendation.get('reason') or suite_recommendation.get('message') or '--'}",
        "",
        "## Scenario Manifest",
        "",
        _markdown_table(scenario_manifest, limit=20),
        "",
        "## Scenario Recommendations",
        "",
        _markdown_table(scenario_recommendations, limit=30),
        "",
        "## Cross-Scenario Profile Ranking",
        "",
        _markdown_table(profile_leaderboard, limit=30),
        "",
        "## Follow-Up",
        "",
        f"- Status: {follow_up.get('status') or 'skipped'}",
        f"- Kind: {follow_up.get('kind') or '--'}",
        f"- Output dir: {follow_up.get('output_dir') or '--'}",
        f"- Message: {follow_up.get('message') or follow_up.get('recommended_variant_id') or '--'}",
        "",
    ]
    (output_dir / "suite_report.md").write_text("\n".join(lines), encoding="utf-8")


def run_research_suite(
    *,
    parquet_base: str,
    date_from: str,
    date_to: str,
    anchor_date_from: Optional[str],
    capital: float,
    output_dir: Path,
    export_profile_trade_ids: Optional[Iterable[str]] = None,
    cost_model: Optional[TradingCostModel] = None,
    run_prod_risk_follow_up: bool = True,
) -> dict[str, Any]:
    effective_cost_model = cost_model or TradingCostModel()
    scenario_defs = default_suite_scenarios(date_from=date_from, date_to=date_to, anchor_date_from=anchor_date_from)
    export_ids = list(export_profile_trade_ids or DEFAULT_EXPORT_PROFILE_TRADE_IDS)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "suite_manifest.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "date_from": date_from,
                "date_to": date_to,
                "anchor_date_from": anchor_date_from,
                "capital": float(capital),
                "export_profile_trade_ids": export_ids,
                "cost_model": effective_cost_model.to_metadata(),
                "scenarios": [asdict(item) for item in scenario_defs],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    tournaments_root = output_dir / "tournaments"
    tournaments_root.mkdir(parents=True, exist_ok=True)

    scenario_rows: list[dict[str, Any]] = []
    scenario_leaderboards: dict[str, pd.DataFrame] = {}
    for scenario in scenario_defs:
        scenario_output_dir = tournaments_root / scenario.name
        result = run_tournament(
            parquet_base=parquet_base,
            date_from=scenario.date_from,
            date_to=scenario.date_to,
            capital=float(capital),
            window_mode=scenario.window_mode,
            output_dir=scenario_output_dir,
            export_profile_trade_ids=export_ids,
            cost_model=effective_cost_model,
        )
        recommendation = json.loads((scenario_output_dir / "recommendation.json").read_text(encoding="utf-8"))
        leaderboard = pd.read_csv(scenario_output_dir / "profile_leaderboard.csv")
        scenario_leaderboards[scenario.name] = leaderboard
        scenario_rows.append(
            {
                "scenario_name": scenario.name,
                "kind": scenario.kind,
                "date_from": scenario.date_from,
                "date_to": scenario.date_to,
                "window_mode": scenario.window_mode,
                "recommended_profile_id": recommendation.get("recommended_profile_id"),
                "recommended_label": recommendation.get("recommended_label"),
                "reason": recommendation.get("reason") or recommendation.get("message"),
                "output_dir": str(scenario_output_dir),
                "windows": int(result.get("windows") or 0),
                "profiles": int(result.get("profiles") or 0),
            }
        )

    scenario_recommendations = pd.DataFrame(scenario_rows)
    scenario_recommendations.to_csv(output_dir / "scenario_recommendations.csv", index=False)

    profile_leaderboard = aggregate_suite_profile_results(scenario_leaderboards)
    profile_leaderboard.to_csv(output_dir / "suite_profile_leaderboard.csv", index=False)

    suite_recommendation = build_suite_recommendation(profile_leaderboard, scenario_recommendations)
    (output_dir / "suite_recommendation.json").write_text(json.dumps(suite_recommendation, indent=2), encoding="utf-8")

    follow_up: dict[str, Any] = {"status": "skipped", "message": "Risk follow-up not triggered."}
    prod_won_any = False
    if not scenario_recommendations.empty:
        prod_won_any = bool(
            (scenario_recommendations["recommended_profile_id"].astype(str) == PROFILE_DET_PROD_V1).any()
        )
    if run_prod_risk_follow_up and prod_won_any:
        sensitivity_output_dir = output_dir / "risk_follow_up_prod_quarterly"
        sensitivity_result = run_sensitivity(
            parquet_base=parquet_base,
            date_from=str(anchor_date_from or date_from),
            date_to=date_to,
            capital=float(capital),
            window_mode="quarterly",
            output_dir=sensitivity_output_dir,
            cost_model=effective_cost_model,
        )
        follow_up = {
            "status": "ok",
            "kind": "prod_risk_sensitivity",
            "output_dir": str(sensitivity_output_dir),
            **sensitivity_result,
        }
    elif run_prod_risk_follow_up:
        follow_up = {
            "status": "skipped",
            "kind": "prod_risk_sensitivity",
            "message": "No tournament scenario recommended det_prod_v1, so prod sensitivity was skipped.",
        }

    (output_dir / "suite_follow_up.json").write_text(json.dumps(follow_up, indent=2), encoding="utf-8")
    _write_suite_report(
        output_dir=output_dir,
        scenarios=scenario_defs,
        scenario_recommendations=scenario_recommendations,
        profile_leaderboard=profile_leaderboard,
        suite_recommendation=suite_recommendation,
        follow_up=follow_up,
        cost_model=effective_cost_model,
    )

    return {
        "output_dir": str(output_dir),
        "scenario_count": len(scenario_defs),
        "suite_recommended_profile_id": suite_recommendation.get("recommended_profile_id"),
        "suite_recommended_label": suite_recommendation.get("recommended_label"),
        "follow_up_status": follow_up.get("status"),
        "follow_up_kind": follow_up.get("kind"),
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a multi-scenario deterministic research suite.")
    parser.add_argument("--parquet-base", default=str(DEFAULT_PARQUET_BASE))
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--anchor-date-from", default=None)
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--export-profile-trades", nargs="*", default=None)
    parser.add_argument("--skip-prod-risk-follow-up", action="store_true")
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
    result = run_research_suite(
        parquet_base=str(args.parquet_base),
        date_from=str(args.date_from),
        date_to=str(args.date_to),
        anchor_date_from=(str(args.anchor_date_from) if args.anchor_date_from else None),
        capital=float(args.capital),
        output_dir=output_dir,
        export_profile_trade_ids=args.export_profile_trades,
        cost_model=cost_model,
        run_prod_risk_follow_up=not bool(args.skip_prod_risk_follow_up),
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
