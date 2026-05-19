"""Model selection pipeline orchestrator.

Reads a recipe matrix config, executes each cell idempotently (skip if
already complete), audits each holdout with the canonical gates, and
emits a leaderboard. Designed to be re-runnable: if interrupted, just
invoke again and it picks up from where it left off.

State files
-----------
  <output_root>/state.json          — pipeline-level state (phase, counts)
  <output_root>/cells/<cell_id>/    — per-cell artifacts (trades, audit, log)
  <output_root>/leaderboard.json    — final ranking when phase=='complete'
  <output_root>/leaderboard.md      — human-readable leaderboard
  <output_root>/pipeline.log        — append-only run log

Idempotency
-----------
  Each cell is identified by its config hash. The orchestrator looks for
  cells/<cell_id>/audit.json and skips that cell if present. To force a
  rerun, delete the cell's directory (or pass --force).

Atomicity
---------
  State writes go via tmp-file + rename; the file is never partially
  written. Cell completion is signalled by audit.json existing.

Usage
-----
    python -m ml_pipeline_2.scripts.model_selection.pipeline \
        --config ml_pipeline_2/scripts/model_selection/recipe_matrix.json \
        --output-root ml_pipeline_2/artifacts/model_selection

Exit codes
----------
    0  pipeline complete (regardless of how many cells passed)
    1  fatal config or filesystem error before any cell ran
    2  a cell raised; pipeline continued but some cells failed (see leaderboard)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


# -----------------------------------------------------------------------------
# Cell + config types
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """A single (recipe, threshold, window, params, features) tuple."""
    recipe_id: str
    threshold: float
    holdout_start: str
    holdout_end: str
    params_source: str  # "default" or path to HPO results.json
    description: str = ""

    @property
    def cell_id(self) -> str:
        """Stable hash-based id (changes only if config changes)."""
        key = f"{self.recipe_id}|{self.threshold}|{self.holdout_start}|{self.holdout_end}|{self.params_source}"
        return f"{self.recipe_id}_thr{int(round(self.threshold * 100))}_{self.holdout_start.replace('-','')}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"


@dataclass
class CellResult:
    cell_id: str
    cell: Dict[str, Any]
    status: str  # "PASS" | "FAIL" | "ERROR" | "SKIPPED"
    audit: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    elapsed_sec: Optional[float] = None


# -----------------------------------------------------------------------------
# State management with atomic writes
# -----------------------------------------------------------------------------


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via tmp + rename so the file is never partial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, path)


def _load_state(state_path: Path) -> Dict[str, Any]:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except Exception:
            logger.warning("state.json present but unreadable; treating as fresh")
    return {
        "started_at": None,
        "updated_at": None,
        "phase": "init",  # init | running | complete | aborted
        "cells_total": 0,
        "cells_completed": 0,
        "cells_passed": 0,
        "cells_failed": 0,
        "cells_errored": 0,
        "results": [],  # list of CellResult dicts
    }


def _save_state(state_path: Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _atomic_write_json(state_path, state)


# -----------------------------------------------------------------------------
# Config loading + cell enumeration
# -----------------------------------------------------------------------------


def load_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"config not found: {config_path}")
    return json.loads(config_path.read_text())


def enumerate_cells(config: Dict[str, Any]) -> List[Cell]:
    """Expand the matrix config into individual cells.

    params_source resolution (in priority order):
      1. entry.params_source_map[recipe]   — explicit per-recipe path (preferred)
      2. entry.params_source_template      — string with {RECIPE} substitution
      3. entry.params_source               — literal value (e.g. "default")
    Fallback when a resolved path doesn't exist: pipeline.py logs a warning
    and uses the trainer's default XGB hyperparameters.
    """
    cells: List[Cell] = []
    matrix = config.get("matrix") or []
    for entry in matrix:
        recipes = entry.get("recipes") or []
        thresholds = entry.get("thresholds") or []
        windows = entry.get("windows") or []
        params_source_map = entry.get("params_source_map") or {}
        params_source_template = entry.get("params_source_template")
        params_source_literal = entry.get("params_source") or "default"
        description = entry.get("description", "")
        for recipe in recipes:
            for thr in thresholds:
                for window in windows:
                    start = window["start"]
                    end = window["end"]
                    # Resolution priority
                    if recipe in params_source_map:
                        ps = params_source_map[recipe]
                    elif params_source_template:
                        ps = params_source_template.replace("{RECIPE}", recipe)
                    else:
                        ps = params_source_literal
                    cells.append(Cell(
                        recipe_id=recipe,
                        threshold=float(thr),
                        holdout_start=start,
                        holdout_end=end,
                        params_source=ps,
                        description=description,
                    ))
    return cells


# -----------------------------------------------------------------------------
# Cell execution
# -----------------------------------------------------------------------------


def _run_cell(
    cell: Cell,
    cell_dir: Path,
    labels_root: str,
    flat_root: str,
    audit_gates: Dict[str, Any],
    repo_root: Path,
) -> CellResult:
    """Execute a single cell: train + extract per-trade parquet + audit.

    Idempotent: if audit.json already exists, returns its content.
    """
    audit_path = cell_dir / "audit.json"
    if audit_path.exists():
        try:
            data = json.loads(audit_path.read_text())
            return CellResult(
                cell_id=cell.cell_id,
                cell=asdict(cell),
                status="PASS" if data.get("passed") else "FAIL",
                audit=data,
                started_at=None,
                completed_at=None,
                elapsed_sec=0.0,
            )
        except Exception:
            logger.warning("cell %s: audit.json unreadable, will re-run", cell.cell_id)

    cell_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    started_iso = datetime.utcnow().isoformat() + "Z"
    log_path = cell_dir / "train.log"

    # Build trainer command
    train_out = cell_dir / "train_out"
    cmd = [
        sys.executable,
        "-m", "ml_pipeline_2.scripts.train_option_pnl_mvp",
        "--labels", labels_root,
        "--flat", flat_root,
        "--out", str(train_out),
        "--recipes", cell.recipe_id,
        "--holdout-end", cell.holdout_end,
    ]
    if cell.params_source and cell.params_source != "default":
        # If params_source is a path that exists, use it; else skip override
        ps = Path(cell.params_source)
        if not ps.is_absolute():
            ps = (repo_root / ps).resolve()
        if ps.exists():
            cmd += ["--params-json", str(ps)]
        else:
            logger.warning("cell %s: params_source missing, using default xgb params: %s", cell.cell_id, ps)

    try:
        with open(log_path, "w") as log_f:
            log_f.write(f"=== {started_iso} ===\nCMD: {' '.join(cmd)}\n\n")
            log_f.flush()
            result = subprocess.run(cmd, stdout=log_f, stderr=subprocess.STDOUT,
                                    cwd=str(repo_root), timeout=3600)
            log_f.write(f"\n=== exit code: {result.returncode} ===\n")
    except subprocess.TimeoutExpired:
        return CellResult(
            cell_id=cell.cell_id, cell=asdict(cell), status="ERROR",
            error="trainer timed out (1h)",
            started_at=started_iso,
            completed_at=datetime.utcnow().isoformat() + "Z",
            elapsed_sec=time.time() - started,
        )

    if result.returncode != 0:
        return CellResult(
            cell_id=cell.cell_id, cell=asdict(cell), status="ERROR",
            error=f"trainer exit code {result.returncode}; see {log_path}",
            started_at=started_iso,
            completed_at=datetime.utcnow().isoformat() + "Z",
            elapsed_sec=time.time() - started,
        )

    # Locate per-trade parquet for this cell's threshold
    thr_int = int(round(cell.threshold * 100))
    trades_path = train_out / "holdout_trades" / cell.recipe_id / f"thr_{thr_int}.parquet"
    if not trades_path.exists():
        return CellResult(
            cell_id=cell.cell_id, cell=asdict(cell), status="ERROR",
            error=f"per-trade parquet missing: {trades_path}",
            started_at=started_iso,
            completed_at=datetime.utcnow().isoformat() + "Z",
            elapsed_sec=time.time() - started,
        )

    # Copy/symlink into cell dir for tidiness
    cell_trades = cell_dir / "trades.parquet"
    if cell_trades.exists():
        cell_trades.unlink()
    try:
        os.link(trades_path, cell_trades)  # hardlink is cheap
    except OSError:
        shutil.copy2(trades_path, cell_trades)

    # Run audit
    audit_cmd = [
        sys.executable,
        str(repo_root / "ml_pipeline_2" / "scripts" / "model_selection" / "audit_run.py"),
        "--trades", str(cell_trades),
        "--return-col", "net_pnl_pct",
        "--date-col", "trade_date",
        "--min-trades", str(audit_gates.get("min_trades", 80)),
        "--max-trades", str(audit_gates.get("max_trades", 500)),
        "--min-win-rate", str(audit_gates.get("min_win_rate", 0.55)),
        "--t-min", str(audit_gates.get("t_min", 2.0)),
        "--output", str(audit_path),
    ]
    if not audit_gates.get("ci_must_exclude_zero", True):
        audit_cmd.append("--allow-ci-include-zero")
    if not audit_gates.get("outlier_survival_must_be_nonneg", True):
        audit_cmd.append("--allow-outlier-driven")

    try:
        with open(log_path, "a") as log_f:
            log_f.write(f"\n=== audit cmd: {' '.join(audit_cmd)} ===\n")
            log_f.flush()
            audit_result = subprocess.run(audit_cmd, stdout=log_f, stderr=subprocess.STDOUT,
                                          cwd=str(repo_root), timeout=300)
    except subprocess.TimeoutExpired:
        return CellResult(
            cell_id=cell.cell_id, cell=asdict(cell), status="ERROR",
            error="audit timed out",
            started_at=started_iso,
            completed_at=datetime.utcnow().isoformat() + "Z",
            elapsed_sec=time.time() - started,
        )

    if not audit_path.exists():
        return CellResult(
            cell_id=cell.cell_id, cell=asdict(cell), status="ERROR",
            error=f"audit did not produce output (exit {audit_result.returncode})",
            started_at=started_iso,
            completed_at=datetime.utcnow().isoformat() + "Z",
            elapsed_sec=time.time() - started,
        )

    audit_data = json.loads(audit_path.read_text())
    return CellResult(
        cell_id=cell.cell_id,
        cell=asdict(cell),
        status="PASS" if audit_data.get("passed") else "FAIL",
        audit=audit_data,
        started_at=started_iso,
        completed_at=datetime.utcnow().isoformat() + "Z",
        elapsed_sec=time.time() - started,
    )


# -----------------------------------------------------------------------------
# Leaderboard
# -----------------------------------------------------------------------------


def _rank_key(r: Dict[str, Any]) -> Tuple[int, float, float]:
    """Sort: PASS first, then t-stat desc, then net_per_trade desc."""
    audit = r.get("audit") or {}
    stats = audit.get("stats") or {}
    is_pass = 1 if r.get("status") == "PASS" else 0
    t = float(stats.get("t") or 0)
    mean = float(stats.get("mean") or 0)
    return (is_pass, t, mean)


def write_leaderboard(output_root: Path, results: List[Dict[str, Any]]) -> None:
    ranked = sorted(results, key=_rank_key, reverse=True)
    leaderboard = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "n_total": len(ranked),
        "n_passed": sum(1 for r in ranked if r.get("status") == "PASS"),
        "n_failed": sum(1 for r in ranked if r.get("status") == "FAIL"),
        "n_errored": sum(1 for r in ranked if r.get("status") == "ERROR"),
        "ranked": ranked,
    }
    _atomic_write_json(output_root / "leaderboard.json", leaderboard)

    # Markdown view
    lines: List[str] = []
    lines.append("# Model selection leaderboard")
    lines.append("")
    lines.append(f"- Generated: {leaderboard['generated_at']}")
    lines.append(f"- Total cells: {leaderboard['n_total']}")
    lines.append(f"- PASS: {leaderboard['n_passed']}  FAIL: {leaderboard['n_failed']}  ERROR: {leaderboard['n_errored']}")
    lines.append("")
    lines.append("| Rank | Status | Recipe | Thr | Holdout | n | t | p | CI 95% | WR | top5 share | net w/o top5 |")
    lines.append("|---:|:---:|:---|---:|:---|---:|---:|---:|:---|---:|---:|---:|")
    for i, r in enumerate(ranked[:50], start=1):
        cell = r.get("cell") or {}
        audit = r.get("audit") or {}
        stats = audit.get("stats") or {}
        ci = audit.get("ci") or {}
        daily = audit.get("daily") or {}
        wr = audit.get("win_rate") or 0
        win = cell.get("holdout_start", "?") + "→" + cell.get("holdout_end", "?")
        t = stats.get("t", 0)
        p = stats.get("p", 1)
        ci_lo = ci.get("ci_lo", 0)
        ci_hi = ci.get("ci_hi", 0)
        top5_share = daily.get("top5_days_share_of_net", 0)
        wo5 = daily.get("net_without_top5_days", 0)
        lines.append(
            f"| {i} | **{r.get('status','?')}** | {cell.get('recipe_id','?')} | "
            f"{cell.get('threshold',0):.2f} | {win} | "
            f"{stats.get('n',0)} | {t:+.2f} | {p:.3f} | "
            f"[{ci_lo*100:+.2f}%,{ci_hi*100:+.2f}%] | "
            f"{wr*100:.1f}% | {top5_share*100:.0f}% | {wo5*100:+.2f}% |"
        )
    (output_root / "leaderboard.md").write_text("\n".join(lines) + "\n")


# -----------------------------------------------------------------------------
# Main orchestration
# -----------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--config", required=True, help="Path to recipe_matrix.json")
    ap.add_argument("--output-root", required=True, help="Directory where state + cells live")
    ap.add_argument("--labels", default="/opt/option_trading/.data/ml_pipeline/parquet_data/option_pnl_labels_v1",
                    help="Path to option-PnL label parquet root")
    ap.add_argument("--flat", default="/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2",
                    help="Path to flat-v2 feature parquet root")
    ap.add_argument("--force", action="store_true", help="Re-run cells even if audit.json exists")
    ap.add_argument("--max-cells", type=int, default=0, help="Limit number of cells (0 = all)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config_path = Path(args.config).resolve()
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    state_path = output_root / "state.json"
    log_path = output_root / "pipeline.log"

    # Append-only run log
    pipeline_log = open(log_path, "a")
    def _log(msg: str) -> None:
        line = f"{datetime.utcnow().isoformat()}Z {msg}\n"
        pipeline_log.write(line); pipeline_log.flush()
        print(line.rstrip())

    _log(f"pipeline start  config={config_path}  output_root={output_root}")

    try:
        config = load_config(config_path)
    except Exception as exc:
        _log(f"FATAL: config load: {exc}")
        return 1

    cells = enumerate_cells(config)
    _log(f"cells enumerated: {len(cells)}")
    if args.max_cells > 0:
        cells = cells[: args.max_cells]
        _log(f"limited to max-cells={args.max_cells}")

    state = _load_state(state_path)
    if state.get("phase") in (None, "init", "aborted", "complete"):
        state["started_at"] = datetime.utcnow().isoformat() + "Z"
        state["phase"] = "running"
        state["cells_total"] = len(cells)
        state["cells_completed"] = 0
        state["cells_passed"] = 0
        state["cells_failed"] = 0
        state["cells_errored"] = 0
        state["results"] = []
    _save_state(state_path, state)

    audit_gates = config.get("audit_gates") or {
        "min_trades": 80, "max_trades": 500, "min_win_rate": 0.55,
        "t_min": 2.0, "ci_must_exclude_zero": True,
        "outlier_survival_must_be_nonneg": True,
    }

    completed_ids = {r["cell_id"] for r in state.get("results") or []}
    n_errors_in_run = 0

    for idx, cell in enumerate(cells, start=1):
        cell_dir = output_root / "cells" / cell.cell_id
        if not args.force and cell.cell_id in completed_ids:
            _log(f"[{idx}/{len(cells)}] SKIP {cell.cell_id} (already in state)")
            continue
        if not args.force and (cell_dir / "audit.json").exists() and cell.cell_id not in completed_ids:
            # restore from disk
            audit_data = json.loads((cell_dir / "audit.json").read_text())
            result = CellResult(
                cell_id=cell.cell_id, cell=asdict(cell),
                status="PASS" if audit_data.get("passed") else "FAIL",
                audit=audit_data, started_at=None, completed_at=None, elapsed_sec=0.0,
            )
            _log(f"[{idx}/{len(cells)}] RESTORE {cell.cell_id} -> {result.status}")
        else:
            _log(f"[{idx}/{len(cells)}] RUN {cell.cell_id}  recipe={cell.recipe_id} thr={cell.threshold} window={cell.holdout_start}..{cell.holdout_end}")
            result = _run_cell(
                cell=cell,
                cell_dir=cell_dir,
                labels_root=args.labels,
                flat_root=args.flat,
                audit_gates=audit_gates,
                repo_root=REPO_ROOT,
            )
            elapsed = result.elapsed_sec or 0
            _log(f"[{idx}/{len(cells)}] DONE  {cell.cell_id}  status={result.status}  elapsed={elapsed:.1f}s")
            if result.status == "ERROR":
                n_errors_in_run += 1
                _log(f"        error: {result.error}")

        state["results"].append(asdict(result))
        state["cells_completed"] = len(state["results"])
        state["cells_passed"] = sum(1 for r in state["results"] if r.get("status") == "PASS")
        state["cells_failed"] = sum(1 for r in state["results"] if r.get("status") == "FAIL")
        state["cells_errored"] = sum(1 for r in state["results"] if r.get("status") == "ERROR")
        _save_state(state_path, state)
        # Update leaderboard live so progress is observable
        write_leaderboard(output_root, state["results"])

    state["phase"] = "complete"
    _save_state(state_path, state)
    write_leaderboard(output_root, state["results"])
    _log(f"pipeline complete  passed={state['cells_passed']}/{state['cells_total']}  errored={state['cells_errored']}")
    pipeline_log.close()
    return 2 if n_errors_in_run > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
