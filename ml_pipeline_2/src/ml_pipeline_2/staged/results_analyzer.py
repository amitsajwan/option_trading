"""Quick result analyzer for staged run summaries.

Usage:
    from ml_pipeline_2.staged.results_analyzer import extract_summary_metrics, compare_runs

    # Extract key metrics from a local or remote summary.json
    metrics = extract_summary_metrics(path_or_dict)
    print(metrics)

    # Compare multiple runs
    comparison = compare_runs([run1_summary, run2_summary])
    print(comparison.to_markdown())
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence


@dataclass(frozen=True)
class RunMetrics:
    run_id: str = ""
    status: str = ""
    publishable: bool = False
    blocking_reasons: list[str] = field(default_factory=list)
    combined_trades: int = 0
    combined_profit_factor: float = 0.0
    combined_net_return: float = 0.0
    combined_max_drawdown: float = 0.0
    combined_long_share: float = 0.0
    combined_short_share: float = 0.0
    selected_recipes: list[str] = field(default_factory=list)
    bypass_stage2: bool = False
    elapsed_minutes: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "publishable": self.publishable,
            "blocking_reasons": self.blocking_reasons,
            "combined_trades": self.combined_trades,
            "combined_profit_factor": round(self.combined_profit_factor, 4),
            "combined_net_return": round(self.combined_net_return, 4),
            "combined_max_drawdown": round(self.combined_max_drawdown, 4),
            "combined_long_share": round(self.combined_long_share, 4),
            "combined_short_share": round(self.combined_short_share, 4),
            "selected_recipes": self.selected_recipes,
            "bypass_stage2": self.bypass_stage2,
        }


def extract_summary_metrics(source: str | Path | dict[str, Any]) -> RunMetrics:
    """Extract key metrics from a summary JSON file or dict.

    Note: CV metrics (roc_auc, brier) are stored in training_report.json
    artifacts, not in summary.json. Use this for policy/holdout metrics.
    """
    if isinstance(source, (str, Path)):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        payload = source

    assess = payload.get("publish_assessment") or {}
    combined = payload.get("combined_holdout_summary") or {}
    stage_artifacts = payload.get("stage_artifacts") or {}
    policy_reports = payload.get("policy_reports") or {}

    # Try to find the selected stage3 policy from policy_reports
    selected_recipes: list[str] = list(combined.get("selected_recipes") or [])
    if not selected_recipes and "stage3" in policy_reports:
        stage3_report = policy_reports["stage3"]
        if isinstance(stage3_report, dict):
            rows = stage3_report.get("validation_rows", [])
            if rows:
                selected_recipes = rows[0].get("selected_recipes", [])

    return RunMetrics(
        run_id=str(payload.get("run_id") or payload.get("output_root", "").split("/")[-1]),
        status=str(payload.get("status", "")),
        publishable=bool(assess.get("publishable", False)),
        blocking_reasons=list(assess.get("blocking_reasons") or []),
        combined_trades=int(combined.get("trades", 0) or 0),
        combined_profit_factor=float(combined.get("profit_factor", 0.0) or 0.0),
        combined_net_return=float(combined.get("net_return_sum", 0.0) or 0.0),
        combined_max_drawdown=float(combined.get("max_drawdown_pct", 0.0) or 0.0),
        combined_long_share=float(combined.get("long_share", 0.0) or 0.0),
        combined_short_share=float(combined.get("short_share", 0.0) or 0.0),
        selected_recipes=selected_recipes,
        bypass_stage2=bool(
            (stage_artifacts.get("stage2") or {}).get("diagnostics", {}).get("bypass_stage2", False)
        ),
    )


def compare_runs(sources: Sequence[str | Path | dict[str, Any]]) -> "RunComparison":
    """Compare multiple runs and return a comparison object."""
    metrics = [extract_summary_metrics(s) for s in sources]
    return RunComparison(metrics)


@dataclass
class RunComparison:
    runs: list[RunMetrics]

    def to_markdown(self) -> str:
        """Return a markdown table comparing all runs."""
        if not self.runs:
            return "No runs to compare."

        lines = [
            "| Run | Status | Pub | Trades | PF | NetRet | DD% | Long% | Bypass | Blockers |",
            "|-----|--------|-----|--------|----|--------|-----|-------|--------|----------|",
        ]
        for r in self.runs:
            lines.append(
                f"| {r.run_id} | {r.status} | {'Y' if r.publishable else 'N'} | "
                f"{r.combined_trades} | {r.combined_profit_factor:.2f} | "
                f"{r.combined_net_return:.4f} | {r.combined_max_drawdown:.2%} | "
                f"{r.combined_long_share:.1%} | "
                f"{'Y' if r.bypass_stage2 else 'N'} | {', '.join(r.blocking_reasons) or '-'} |"
            )
        return "\n".join(lines)

    def best_by(self, key: str) -> RunMetrics | None:
        """Return the best run by a given metric key."""
        if not self.runs:
            return None
        if key == "net_return":
            return max(self.runs, key=lambda r: r.combined_net_return)
        if key == "profit_factor":
            return max(self.runs, key=lambda r: r.combined_profit_factor)
        if key == "trades":
            return max(self.runs, key=lambda r: r.combined_trades)
        raise ValueError(f"Unknown comparison key: {key}")


__all__ = ["RunMetrics", "extract_summary_metrics", "compare_runs", "RunComparison"]
