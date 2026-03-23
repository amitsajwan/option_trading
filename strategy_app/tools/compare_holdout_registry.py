"""Compare deterministic holdout registries against a frozen baseline row."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _top_row(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        raise ValueError("holdout registry is empty")
    work = frame.copy()
    if "accepted" in work.columns:
        accepted = work[work["accepted"].astype(str).str.lower().isin({"true", "1", "yes"})].copy()
        if not accepted.empty:
            return accepted.iloc[0].to_dict()
    return work.iloc[0].to_dict()


def _as_float(row: dict[str, Any], key: str) -> float:
    try:
        value = pd.to_numeric(row.get(key), errors="coerce")
        if pd.isna(value):
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def compare_holdout_registries(
    *,
    baseline_holdout_registry: Path,
    candidate_holdout_registry: Path,
    output_dir: Path,
    min_return_delta_pp: float = -0.10,
    max_drawdown_multiple: float = 1.10,
    min_trade_ratio: float = 0.80,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline_df = pd.read_csv(baseline_holdout_registry)
    candidate_df = pd.read_csv(candidate_holdout_registry)
    baseline_row = _top_row(baseline_df)
    candidate_row = _top_row(candidate_df)

    baseline_return = _as_float(baseline_row, "net_capital_return_pct")
    candidate_return = _as_float(candidate_row, "net_capital_return_pct")
    baseline_dd = abs(_as_float(baseline_row, "max_drawdown_pct"))
    candidate_dd = abs(_as_float(candidate_row, "max_drawdown_pct"))
    baseline_trades = _as_float(baseline_row, "trades")
    candidate_trades = _as_float(candidate_row, "trades")

    return_delta_pp = (candidate_return - baseline_return) * 100.0
    trade_ratio = (
        float(candidate_trades / baseline_trades)
        if baseline_trades > 0
        else float("nan")
    )
    dd_ratio = (
        float(candidate_dd / baseline_dd)
        if baseline_dd > 0
        else float("nan")
    )

    return_gate = bool(return_delta_pp >= float(min_return_delta_pp))
    drawdown_gate = bool((candidate_dd <= baseline_dd * float(max_drawdown_multiple)) if baseline_dd > 0 else True)
    trade_gate = bool((candidate_trades >= baseline_trades * float(min_trade_ratio)) if baseline_trades > 0 else True)
    passed = bool(return_gate and drawdown_gate and trade_gate)

    reasons: list[str] = []
    if not return_gate:
        reasons.append("return_gate_failed")
    if not drawdown_gate:
        reasons.append("drawdown_gate_failed")
    if not trade_gate:
        reasons.append("trade_count_gate_failed")

    rows = pd.DataFrame(
        [
            {"registry": "baseline", **baseline_row},
            {"registry": "candidate", **candidate_row},
        ]
    )
    rows.to_csv(output_dir / "comparison_rows.csv", index=False)

    summary = {
        "generated_at_utc": _utc_now(),
        "baseline_holdout_registry": str(baseline_holdout_registry).replace("\\", "/"),
        "candidate_holdout_registry": str(candidate_holdout_registry).replace("\\", "/"),
        "gates": {
            "min_return_delta_pp": float(min_return_delta_pp),
            "max_drawdown_multiple": float(max_drawdown_multiple),
            "min_trade_ratio": float(min_trade_ratio),
        },
        "metrics": {
            "baseline_candidate_id": baseline_row.get("candidate_id"),
            "candidate_candidate_id": candidate_row.get("candidate_id"),
            "baseline_return_pct": baseline_return,
            "candidate_return_pct": candidate_return,
            "return_delta_pp": return_delta_pp,
            "baseline_drawdown_abs_pct": baseline_dd,
            "candidate_drawdown_abs_pct": candidate_dd,
            "drawdown_ratio": dd_ratio,
            "baseline_trades": baseline_trades,
            "candidate_trades": candidate_trades,
            "trade_ratio": trade_ratio,
        },
        "gate_results": {
            "return_gate": return_gate,
            "drawdown_gate": drawdown_gate,
            "trade_count_gate": trade_gate,
            "passed": passed,
            "reasons": reasons,
        },
    }
    (output_dir / "comparison_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare deterministic holdout registry against a frozen baseline.")
    parser.add_argument("--baseline-holdout-registry", required=True)
    parser.add_argument("--candidate-holdout-registry", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--min-return-delta-pp", type=float, default=-0.10)
    parser.add_argument("--max-drawdown-multiple", type=float, default=1.10)
    parser.add_argument("--min-trade-ratio", type=float, default=0.80)
    args = parser.parse_args(list(argv) if argv is not None else None)

    summary = compare_holdout_registries(
        baseline_holdout_registry=Path(args.baseline_holdout_registry),
        candidate_holdout_registry=Path(args.candidate_holdout_registry),
        output_dir=Path(args.output_dir),
        min_return_delta_pp=float(args.min_return_delta_pp),
        max_drawdown_multiple=float(args.max_drawdown_multiple),
        min_trade_ratio=float(args.min_trade_ratio),
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
