"""Rules pipeline orchestrator — run a matrix of (rule × window × exit_mode)
cells through the backtest + audit, then emit a leaderboard.

Mirrors the shape of model_selection/pipeline.py but lighter:
  - No subprocess per cell (run_backtest is called as a function).
  - No daemon wrapper (run synchronously; outer cron/tmux is the caller).
  - Idempotent: a cell with audit.json already present is skipped unless
    --force is passed.

Config (rule_matrix.json):
{
  "rules": [
    {"rule_id": "R1", "path": "ml_pipeline_2/configs/rules/r1_orb.json"},
    ...
  ],
  "windows": [
    {"name": "may_jul_2024", "start": "2024-05-01", "end": "2024-07-31"},
    {"name": "aug_oct_2024", "start": "2024-08-01", "end": "2024-10-31"}
  ],
  "exit_modes": ["mechanical"],
  "audit_thresholds": {
      "min_trades": 30, "t_min": 2.0, "min_win_rate": 0.40,
      "ci_must_exclude_zero": true,
      "outlier_survival_must_be_nonneg": true
  }
}

Usage:
    python -m ml_pipeline_2.scripts.rules_pipeline.pipeline \\
        --config ml_pipeline_2/scripts/rules_pipeline/rule_matrix.json \\
        --output-root ml_pipeline_2/artifacts/rules_runs/run_$(date +%Y%m%d)

Outputs in <output_root>:
    cells/<cell_id>/        per-cell artifacts (rule.json, trades.parquet,
                            audit.json, summary.txt)
    leaderboard.json        machine-readable ranking
    leaderboard.md          human-readable
    pipeline.log            run log
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Cell:
    rule_id: str
    rule_path: str
    window_name: str
    start: str
    end: str
    exit_mode: str

    @property
    def cell_id(self) -> str:
        return f"{self.rule_id}_{self.window_name}_{self.exit_mode}"


def enumerate_cells(cfg: Dict[str, Any]) -> List[Cell]:
    cells: List[Cell] = []
    exit_modes = cfg.get("exit_modes") or ["mechanical"]
    for rule_entry in cfg["rules"]:
        for window in cfg["windows"]:
            for exit_mode in exit_modes:
                cells.append(Cell(
                    rule_id=rule_entry["rule_id"],
                    rule_path=rule_entry["path"],
                    window_name=window["name"],
                    start=window["start"],
                    end=window["end"],
                    exit_mode=exit_mode,
                ))
    return cells


def _load_rule(rule_path: Path) -> Dict[str, Any]:
    if not rule_path.is_absolute():
        rule_path = (REPO_ROOT / rule_path).resolve()
    if not rule_path.exists():
        raise FileNotFoundError(f"rule file not found: {rule_path}")
    return json.loads(rule_path.read_text())


def run_pipeline(
    config_path: Path,
    output_root: Path,
    *,
    force: bool = False,
    max_cells: int = 0,
) -> int:
    from .run_backtest import run_backtest

    cfg = json.loads(config_path.read_text())
    output_root.mkdir(parents=True, exist_ok=True)
    cells_dir = output_root / "cells"
    cells_dir.mkdir(exist_ok=True)

    cells = enumerate_cells(cfg)
    if max_cells > 0:
        cells = cells[:max_cells]

    audit_thresholds = cfg.get("audit_thresholds") or {}
    results: List[Tuple[Cell, Dict[str, Any]]] = []
    errored = 0

    for i, cell in enumerate(cells, 1):
        cell_dir = cells_dir / cell.cell_id
        audit_path = cell_dir / "audit.json"

        if audit_path.exists() and not force:
            logger.info("[%d/%d] skip %s (audit.json present)", i, len(cells), cell.cell_id)
            result = json.loads(audit_path.read_text())
            results.append((cell, result))
            continue

        logger.info("[%d/%d] running %s", i, len(cells), cell.cell_id)
        try:
            rule_dict = _load_rule(Path(cell.rule_path))
            result = run_backtest(
                rule_dict=rule_dict,
                start_date=cell.start,
                end_date=cell.end,
                output_dir=cell_dir,
                exit_mode=cell.exit_mode,
                audit_thresholds=audit_thresholds,
            )
            results.append((cell, result))
        except Exception as exc:
            logger.exception("[%d/%d] cell %s ERRORED", i, len(cells), cell.cell_id)
            errored += 1
            err_result = {"available": False, "passed": False, "errored": True, "error": str(exc)}
            cell_dir.mkdir(parents=True, exist_ok=True)
            (cell_dir / "error.txt").write_text(str(exc))
            results.append((cell, err_result))

    _write_leaderboard(output_root, results)
    n_pass = sum(1 for _, r in results if r.get("passed"))
    n_fail = sum(1 for _, r in results if r.get("available", True) and not r.get("passed") and not r.get("errored"))
    logger.info("pipeline complete: %d cells, %d PASS, %d FAIL, %d ERROR",
                len(results), n_pass, n_fail, errored)
    return 0


def _write_leaderboard(output_root: Path, results: List[Tuple[Cell, Dict[str, Any]]]) -> None:
    """Sort by t-stat desc among PASS, then by t-stat desc among FAIL."""
    def sort_key(item):
        cell, r = item
        passed = bool(r.get("passed"))
        t = float((r.get("stats") or {}).get("t", 0.0))
        return (0 if passed else 1, -t)

    ranked = sorted(results, key=sort_key)

    rows_json = []
    for cell, r in ranked:
        stats = r.get("stats") or {}
        ci = r.get("ci") or {}
        daily = r.get("daily") or {}
        rows_json.append({
            "cell_id": cell.cell_id,
            "rule_id": cell.rule_id,
            "window": cell.window_name,
            "start": cell.start, "end": cell.end,
            "exit_mode": cell.exit_mode,
            "passed": bool(r.get("passed")),
            "errored": bool(r.get("errored")),
            "n_trades": r.get("n_trades", r.get("n_trades_emitted", 0)),
            "t": stats.get("t"),
            "p": stats.get("p"),
            "ci_lo": ci.get("ci_lo"),
            "ci_hi": ci.get("ci_hi"),
            "win_rate": r.get("win_rate"),
            "top5_days_share": daily.get("top5_days_share_of_net"),
            "net_without_top5": daily.get("net_without_top5_days"),
        })
    (output_root / "leaderboard.json").write_text(json.dumps(rows_json, indent=2, default=str))

    lines = []
    n_pass = sum(1 for r in rows_json if r["passed"])
    n_err = sum(1 for r in rows_json if r["errored"])
    n_fail = len(rows_json) - n_pass - n_err
    lines.append("# Rules pipeline leaderboard")
    lines.append("")
    lines.append(f"- Total cells: {len(rows_json)}")
    lines.append(f"- PASS: {n_pass}  FAIL: {n_fail}  ERROR: {n_err}")
    lines.append("")
    lines.append("| Rank | Status | Rule | Window | n | t | p | CI 95% | WR | top5 share | net w/o top5 |")
    lines.append("|---:|:---:|:---|:---|---:|---:|---:|:---|---:|---:|---:|")
    for i, r in enumerate(rows_json, 1):
        status = "**ERROR**" if r["errored"] else ("**PASS**" if r["passed"] else "**FAIL**")
        t = r["t"] if r["t"] is not None else 0.0
        p = r["p"] if r["p"] is not None else 1.0
        ci_lo = (r["ci_lo"] or 0.0) * 100
        ci_hi = (r["ci_hi"] or 0.0) * 100
        wr = (r["win_rate"] or 0.0) * 100
        t5 = (r["top5_days_share"] or 0.0) * 100
        nwt = (r["net_without_top5"] or 0.0) * 100
        lines.append(
            f"| {i} | {status} | {r['rule_id']} | {r['window']} | {r['n_trades']} "
            f"| {t:+.2f} | {p:.3f} | [{ci_lo:+.2f}%,{ci_hi:+.2f}%] "
            f"| {wr:.1f}% | {t5:.0f}% | {nwt:+.2f}% |"
        )
    (output_root / "leaderboard.md").write_text("\n".join(lines) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--config", required=True, help="Path to rule_matrix.json")
    p.add_argument("--output-root", required=True, help="Directory for state + cells")
    p.add_argument("--force", action="store_true", help="Re-run cells even if audit.json exists")
    p.add_argument("--max-cells", type=int, default=0, help="Limit number of cells (0 = all)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"FATAL: config not found: {config_path}", file=sys.stderr)
        return 1

    output_root = Path(args.output_root).resolve()
    return run_pipeline(config_path, output_root, force=args.force, max_cells=args.max_cells)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
