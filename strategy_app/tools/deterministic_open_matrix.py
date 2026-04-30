"""Open-matrix deterministic search for manifest-gated rebaseline cycles."""

from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
from snapshot_app.historical.window_manifest import (
    DEFAULT_MIN_TRADING_DAYS,
    DEFAULT_REQUIRED_SCHEMA_VERSION,
    load_and_validate_window_manifest,
    split_boundaries_for_days,
)
from strategy_app.engines.strategy_router import StrategyRouter
from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.tools.offline_strategy_analysis import MemorySignalLogger, _group_table, _summary

DEFAULT_PARQUET_BASE = DEFAULT_HISTORICAL_PARQUET_BASE
DEFAULT_OUTPUT_ROOT = Path(".run/deterministic_open_matrix")
DEFAULT_CAPITAL = 500000.0


@dataclass(frozen=True)
class DeterministicCandidateConfig:
    candidate_id: str
    metadata: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_snapshots(store: ParquetStore, start_date: str, end_date: str) -> pd.DataFrame:
    df = store.snapshots_for_date_range(start_date, end_date)
    if df.empty:
        return df
    return df.loc[:, ["trade_date", "timestamp", "snapshot_raw_json"]].copy()


def _powerset(items: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    for r in range(1, len(items) + 1):
        out.extend([list(combo) for combo in itertools.combinations(items, r)])
    return out


def _default_search_spec() -> dict[str, Any]:
    try:
        router = StrategyRouter()
        core = [
            name
            for name in router.available_strategy_names()
            if name not in {"IV_FILTER", "HIGH_VOL_ORB"}
        ]
    except Exception:
        core = ["ORB", "EMA_CROSSOVER", "VWAP_RECLAIM", "OI_BUILDUP", "PREV_DAY_LEVEL"]
    core = sorted(set(core))
    strategy_sets = _powerset(core)
    return {
        "risk_profiles": [
            {"name": "risk_default", "risk_config": {}},
            {"name": "risk_tight_10", "risk_config": {"stop_loss_pct": 0.10, "target_pct": 0.80, "trailing_enabled": False}},
            {"name": "risk_medium_20", "risk_config": {"stop_loss_pct": 0.20, "target_pct": 0.80, "trailing_enabled": False}},
            {
                "name": "risk_tight_10_trail",
                "risk_config": {
                    "stop_loss_pct": 0.10,
                    "target_pct": 0.80,
                    "trailing_enabled": True,
                    "trailing_activation_pct": 0.10,
                    "trailing_offset_pct": 0.05,
                    "trailing_lock_breakeven": True,
                },
            },
            {
                "name": "risk_medium_20_trail",
                "risk_config": {
                    "stop_loss_pct": 0.20,
                    "target_pct": 0.80,
                    "trailing_enabled": True,
                    "trailing_activation_pct": 0.15,
                    "trailing_offset_pct": 0.07,
                    "trailing_lock_breakeven": True,
                },
            },
        ],
        "regime_profiles": [
            {"name": "regime_default", "regime_config": {}},
            {
                "name": "regime_loose_trend",
                "regime_config": {"trend_return_min": 0.0008, "trend_vol_ratio_min": 1.20, "high_vol_vix_min": 23.0},
            },
            {
                "name": "regime_strict_trend",
                "regime_config": {"trend_return_min": 0.0015, "trend_vol_ratio_min": 1.40, "high_vol_vix_min": 21.0},
            },
        ],
        "strategy_sets": [{"name": f"set_{'+'.join(items)}", "strategies": items} for items in strategy_sets],
    }


def _load_search_spec(path: Optional[str]) -> dict[str, Any]:
    if not path:
        return _default_search_spec()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("deterministic search spec must be a JSON object")
    return payload


def _candidate_matrix(search_spec: dict[str, Any]) -> list[DeterministicCandidateConfig]:
    candidates: list[DeterministicCandidateConfig] = [
        DeterministicCandidateConfig(
            candidate_id="baseline_default",
            metadata={
                "risk_config": {},
                "regime_config": {},
                "router_config": {},
                "strategy_profile_id": "det_core_v2",
            },
        )
    ]
    risk_profiles = [item for item in (search_spec.get("risk_profiles") or []) if isinstance(item, dict)]
    regime_profiles = [item for item in (search_spec.get("regime_profiles") or []) if isinstance(item, dict)]
    strategy_sets = [item for item in (search_spec.get("strategy_sets") or []) if isinstance(item, dict)]
    if not risk_profiles:
        risk_profiles = [{"name": "risk_default", "risk_config": {}}]
    if not regime_profiles:
        regime_profiles = [{"name": "regime_default", "regime_config": {}}]
    if not strategy_sets:
        strategy_sets = [{"name": "set_all", "strategies": ["ORB", "EMA_CROSSOVER", "VWAP_RECLAIM", "OI_BUILDUP", "PREV_DAY_LEVEL"]}]

    for risk in risk_profiles:
        for regime in regime_profiles:
            for strategy_set in strategy_sets:
                enabled = ["IV_FILTER", *[str(name).strip().upper() for name in list(strategy_set.get("strategies") or [])]]
                enabled = sorted(set([name for name in enabled if name]))
                router_overrides = dict(strategy_set.get("router_config") or {})
                router_config = {"enabled_entry_strategies": enabled}
                router_config.update(router_overrides)
                cid = f"{str(risk.get('name') or 'risk')}_{str(regime.get('name') or 'regime')}_{str(strategy_set.get('name') or 'set')}"
                if cid == "baseline_default":
                    continue
                candidates.append(
                    DeterministicCandidateConfig(
                        candidate_id=cid,
                        metadata={
                            "risk_config": dict(risk.get("risk_config") or {}),
                            "regime_config": dict(regime.get("regime_config") or {}),
                            "router_config": router_config,
                            "strategy_profile_id": f"det_{cid}",
                        },
                    )
                )
    return candidates


def _run_candidate(
    *,
    snapshots: pd.DataFrame,
    candidate: DeterministicCandidateConfig,
    capital_allocated: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    logger = MemorySignalLogger(capital_allocated=capital_allocated)
    engine = DeterministicRuleEngine(signal_logger=logger)
    engine.set_run_context(f"det-open-{candidate.candidate_id}", candidate.metadata)
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
    trades = pd.DataFrame(logger.trades)
    if not trades.empty:
        trades["entry_time"] = pd.to_datetime(trades["entry_time"], errors="coerce")
        trades["exit_time"] = pd.to_datetime(trades["exit_time"], errors="coerce")
        trades = trades.sort_values(["exit_time", "position_id"], kind="stable").reset_index(drop=True)
    summary = _summary(trades, capital_allocated=capital_allocated)
    summary["candidate_id"] = candidate.candidate_id
    return trades, summary


def _gate_vs_baseline(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    return _gate_vs_baseline_configured(
        candidate,
        baseline,
        require_positive_return=False,
        min_outperformance_pct=0.0,
    )


def _gate_vs_baseline_configured(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    *,
    require_positive_return: bool,
    min_outperformance_pct: float,
) -> dict[str, Any]:
    cand_ret = float(candidate.get("net_capital_return_pct") or 0.0)
    base_ret = float(baseline.get("net_capital_return_pct") or 0.0)
    cand_dd = float(candidate.get("max_drawdown_pct") or 0.0)
    base_dd = float(baseline.get("max_drawdown_pct") or 0.0)
    cand_trades = float(candidate.get("trades") or 0.0)
    base_trades = float(baseline.get("trades") or 0.0)
    return_gate = cand_ret > (base_ret + float(min_outperformance_pct))
    positive_return_gate = (cand_ret > 0.0) if bool(require_positive_return) else True
    drawdown_gate = cand_dd >= (base_dd * 1.15 if base_dd < 0 else base_dd)
    trade_gate = cand_trades >= (base_trades * 0.70 if base_trades > 0 else 0.0)
    passed = bool(return_gate and positive_return_gate and drawdown_gate and trade_gate)
    reasons: list[str] = []
    if not return_gate:
        reasons.append("return_gate_failed")
    if not positive_return_gate:
        reasons.append("positive_return_gate_failed")
    if not drawdown_gate:
        reasons.append("drawdown_gate_failed")
    if not trade_gate:
        reasons.append("trade_count_gate_failed")
    return {
        "return_gate": return_gate,
        "positive_return_gate": positive_return_gate,
        "drawdown_gate": drawdown_gate,
        "trade_count_gate": trade_gate,
        "accepted": passed,
        "gate_reasons": reasons,
    }


def _select_preferred_candidate_row(
    ranked_df: pd.DataFrame,
    *,
    baseline_candidate_id: str = "baseline_default",
    require_accepted: bool = False,
) -> tuple[dict[str, Any], str]:
    accepted_df = ranked_df[ranked_df["accepted"] == True].copy()
    if not accepted_df.empty:
        return accepted_df.iloc[0].to_dict(), "accepted_rank_1"
    if bool(require_accepted):
        raise ValueError("no accepted deterministic candidates available under active gates")

    baseline_df = ranked_df[ranked_df["candidate_id"] == baseline_candidate_id].copy()
    if not baseline_df.empty:
        return baseline_df.iloc[0].to_dict(), "baseline_fallback"

    return ranked_df.iloc[0].to_dict(), "top_rank_fallback"


def run_deterministic_open_matrix(
    *,
    parquet_base: Path,
    start_date: str,
    end_date: str,
    split_boundaries: dict[str, str],
    output_dir: Path,
    capital: float,
    search_spec_path: Optional[str] = None,
    run_meta: Optional[dict[str, Any]] = None,
    require_positive_return: bool = False,
    min_outperformance_pct: float = 0.005,
    require_accepted_holdout: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_CANONICAL,
        context="deterministic_open_matrix",
        parquet_base=parquet_base,
        min_day=str(split_boundaries["valid_start"]),
        max_day=str(split_boundaries["eval_end"]),
    )
    store = ParquetStore(parquet_base, snapshots_dataset=SNAPSHOT_DATASET_CANONICAL)

    valid_start = str(split_boundaries["valid_start"])
    valid_end = str(split_boundaries["valid_end"])
    eval_start = str(split_boundaries["eval_start"])
    eval_end = str(split_boundaries["eval_end"])
    valid_snapshots = _load_snapshots(store, valid_start, valid_end)
    holdout_snapshots = _load_snapshots(store, eval_start, eval_end)
    if valid_snapshots.empty or holdout_snapshots.empty:
        raise ValueError("insufficient snapshots for deterministic matrix valid/holdout")

    search_spec = _load_search_spec(search_spec_path)
    candidates = _candidate_matrix(search_spec)
    valid_rows: list[dict[str, Any]] = []
    per_candidate_valid_trades: dict[str, pd.DataFrame] = {}
    for candidate in candidates:
        trades, summary = _run_candidate(snapshots=valid_snapshots, candidate=candidate, capital_allocated=capital)
        per_candidate_valid_trades[candidate.candidate_id] = trades
        summary["candidate_id"] = candidate.candidate_id
        summary["metadata"] = candidate.metadata
        valid_rows.append(summary)

    valid_df = pd.DataFrame(valid_rows)
    if valid_df.empty:
        raise ValueError("deterministic matrix produced no valid summaries")
    baseline_row = valid_df[valid_df["candidate_id"] == "baseline_default"]
    if baseline_row.empty:
        raise ValueError("baseline_default missing from deterministic matrix")
    baseline_summary = baseline_row.iloc[0].to_dict()

    gate_records: list[dict[str, Any]] = []
    for row in valid_rows:
        gates = _gate_vs_baseline_configured(
            row,
            baseline_summary,
            require_positive_return=bool(require_positive_return),
            min_outperformance_pct=float(min_outperformance_pct),
        )
        gate_records.append({"candidate_id": row["candidate_id"], **gates})
    gate_df = pd.DataFrame(gate_records)
    valid_df = valid_df.merge(gate_df, on="candidate_id", how="left")
    valid_df = valid_df.sort_values(
        ["accepted", "net_capital_return_pct", "max_drawdown_pct", "profit_factor", "trades"],
        ascending=[False, False, False, False, False],
        kind="stable",
    )
    valid_df.to_csv(output_dir / "valid_registry.csv", index=False)

    valid_comparator_row, valid_comparator_selection_reason = _select_preferred_candidate_row(valid_df)
    valid_comparator = {
        "candidate_id": str(valid_comparator_row["candidate_id"]),
        "metadata": valid_comparator_row.get("metadata"),
        "summary": valid_comparator_row,
        "selection_reason": valid_comparator_selection_reason,
    }
    (output_dir / "valid_comparator.json").write_text(json.dumps(valid_comparator, indent=2, default=str), encoding="utf-8")

    holdout_rows: list[dict[str, Any]] = []
    per_candidate_holdout_trades: dict[str, pd.DataFrame] = {}
    candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
    for candidate_id in [candidate.candidate_id for candidate in candidates]:
        candidate = candidate_map[candidate_id]
        trades, summary = _run_candidate(snapshots=holdout_snapshots, candidate=candidate, capital_allocated=capital)
        per_candidate_holdout_trades[candidate_id] = trades
        summary["candidate_id"] = candidate_id
        summary["metadata"] = candidate.metadata
        holdout_rows.append(summary)
        trades.to_csv(output_dir / f"holdout_trades_{candidate_id}.csv", index=False)

    holdout_df = pd.DataFrame(holdout_rows)
    holdout_baseline_summary = holdout_df[holdout_df["candidate_id"] == "baseline_default"].iloc[0].to_dict()
    holdout_gates: list[dict[str, Any]] = []
    for row in holdout_rows:
        gates = _gate_vs_baseline_configured(
            row,
            holdout_baseline_summary,
            require_positive_return=bool(require_positive_return),
            min_outperformance_pct=float(min_outperformance_pct),
        )
        holdout_gates.append({"candidate_id": row["candidate_id"], **gates})
    holdout_df = holdout_df.merge(pd.DataFrame(holdout_gates), on="candidate_id", how="left")
    holdout_df = holdout_df.sort_values(
        ["accepted", "net_capital_return_pct", "max_drawdown_pct", "profit_factor", "trades"],
        ascending=[False, False, False, False, False],
        kind="stable",
    )
    holdout_df.to_csv(output_dir / "holdout_registry.csv", index=False)

    champion_row, champion_selection_reason = _select_preferred_candidate_row(
        holdout_df,
        require_accepted=bool(require_accepted_holdout),
    )
    champion = {
        "candidate_id": str(champion_row["candidate_id"]),
        "summary": champion_row,
        "metadata": champion_row.get("metadata"),
        "selection_reason": champion_selection_reason,
    }
    (output_dir / "champion.json").write_text(json.dumps(champion, indent=2, default=str), encoding="utf-8")

    summary = {
        "created_at_utc": _utc_now(),
        "start_date": start_date,
        "end_date": end_date,
        "split_boundaries": split_boundaries,
        **snapshot_access.to_metadata(),
        "candidate_count": int(len(candidates)),
        "valid_registry_csv": str((output_dir / "valid_registry.csv")).replace("\\", "/"),
        "holdout_registry_csv": str((output_dir / "holdout_registry.csv")).replace("\\", "/"),
        "valid_comparator_json": str((output_dir / "valid_comparator.json")).replace("\\", "/"),
        "champion_json": str((output_dir / "champion.json")).replace("\\", "/"),
        "deterministic_champion_id": champion["candidate_id"],
        "deterministic_valid_comparator_id": valid_comparator["candidate_id"],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if run_meta is not None:
        (output_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
    return summary


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic open-matrix search on a manifest window.")
    parser.add_argument("--parquet-base", default=str(DEFAULT_PARQUET_BASE))
    parser.add_argument("--window-manifest", required=True)
    parser.add_argument("--formal-run", action="store_true")
    parser.add_argument("--search-spec", default=None)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--require-positive-return", action="store_true")
    parser.add_argument("--min-outperformance-pct", type=float, default=0.005)
    parser.add_argument("--require-accepted-holdout", action="store_true")
    parser.add_argument("--manifest-min-trading-days", type=int, default=DEFAULT_MIN_TRADING_DAYS)
    parser.add_argument("--manifest-required-schema-version", default=DEFAULT_REQUIRED_SCHEMA_VERSION)
    args = parser.parse_args(list(argv) if argv is not None else None)

    manifest_meta = load_and_validate_window_manifest(
        args.window_manifest,
        formal_run=bool(args.formal_run),
        required_schema_version=str(args.manifest_required_schema_version),
        min_trading_days=int(args.manifest_min_trading_days),
        context="deterministic_open_matrix.window_manifest",
    )
    snapshot_access = require_snapshot_access(
        mode=SNAPSHOT_INPUT_MODE_CANONICAL,
        context="deterministic_open_matrix",
        parquet_base=Path(args.parquet_base),
        min_day=str(manifest_meta["window_start"]),
        max_day=str(manifest_meta["window_end"]),
    )
    store = ParquetStore(Path(args.parquet_base), snapshots_dataset=SNAPSHOT_DATASET_CANONICAL)
    days = store.available_snapshot_days(str(manifest_meta["window_start"]), str(manifest_meta["window_end"]))
    split = split_boundaries_for_days(days)
    run_meta = {
        "generated_at_utc": _utc_now(),
        "command": "strategy_app.tools.deterministic_open_matrix",
        "formal_run": bool(args.formal_run),
        "exploratory_only": bool((manifest_meta or {}).get("exploratory_only", not bool(args.formal_run))),
        "window_manifest": manifest_meta,
        "manifest_path": manifest_meta.get("manifest_path"),
        "manifest_hash": manifest_meta.get("manifest_hash"),
        "window_start": manifest_meta["window_start"],
        "window_end": manifest_meta["window_end"],
        **snapshot_access.to_metadata(),
        "split_boundaries": split,
        "gate_results": {
            "formal_ready": manifest_meta.get("formal_ready"),
            "required_schema_version": str(args.manifest_required_schema_version),
            "min_trading_days_required": int(args.manifest_min_trading_days),
            "window_trading_days": manifest_meta.get("trading_days"),
            "all_days_required_schema": manifest_meta.get("all_days_required_schema"),
        },
    }
    result = run_deterministic_open_matrix(
        parquet_base=Path(args.parquet_base),
        start_date=str(manifest_meta["window_start"]),
        end_date=str(manifest_meta["window_end"]),
        split_boundaries=split,
        output_dir=Path(args.output_dir),
        capital=float(args.capital),
        search_spec_path=args.search_spec,
        run_meta=run_meta,
        require_positive_return=bool(args.require_positive_return),
        min_outperformance_pct=float(args.min_outperformance_pct),
        require_accepted_holdout=bool(args.require_accepted_holdout),
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
