"""
update_grid_manifest.py — Automation helper.

Reads completed run summaries, picks the best run, and patches the next
grid's base manifest so the experiment chain continues without manual work.

Two modes:

1. Individual summaries (Grid A → Grid B):
   Reads 3 standalone summary.json files and picks the best one.

   python tools/update_grid_manifest.py \\
       --run-summaries  path/a1/summary.json path/a2/summary.json path/a3/summary.json \\
       --base-manifest  configs/research/staged_dual_recipe.label_fix_b_base.json \\
       --grid-kind      label_fix \\
       [--dry-run]

2. Grid summary (Grid B → Grid C):
   Reads a grid_summary.json produced by run_staged_grid.

   python tools/update_grid_manifest.py \\
       --grid-summary   path/to/grid_summary.json \\
       --base-manifest  configs/research/staged_dual_recipe.deep_hpo_base.json \\
       --grid-kind      feature_s2 \\
       [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_str(val: Any, default: str = "") -> str:
    return str(val) if val is not None else default


def _deep_set(d: Dict[str, Any], keys: List[str], value: Any) -> None:
    """Set a nested key path in a dict, creating sub-dicts as needed."""
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


# ---------------------------------------------------------------------------
# Run scoring
# ---------------------------------------------------------------------------

def _score_for_label_fix(run: Dict[str, Any]) -> tuple[float, ...]:
    """
    Grid A scorer: prefer runs where:
    1. S2 CV ROC >= 0.55 (hard minimum)
    2. long_share is closest to 0.5 (bias fix)
    3. PF is highest (secondary)
    """
    summary_path = run.get("summary_path")
    if not summary_path or not Path(summary_path).exists():
        return (-1.0, float("inf"), float("-inf"))
    summary = _load(Path(summary_path))
    combined = dict(summary.get("combined_holdout") or {})
    stage_quality = dict(summary.get("stage_quality") or {})
    s2_quality = dict(stage_quality.get("stage2") or {})
    s2_roc = _safe_float(s2_quality.get("roc_auc"), default=0.0)
    long_share = _safe_float(combined.get("long_share"), default=0.5)
    pf = _safe_float(combined.get("profit_factor"), default=0.0)
    roc_ok = 1.0 if s2_roc >= 0.55 else 0.0
    bias_distance = abs(long_share - 0.5)
    return (roc_ok, -bias_distance, pf)


def _score_for_feature_s2(run: Dict[str, Any]) -> tuple[float, ...]:
    """
    Grid B scorer: best feature set = highest holdout PF with trades >= 50
    and long_share in 30-70%.
    """
    summary_path = run.get("summary_path")
    if not summary_path or not Path(summary_path).exists():
        return (float("-inf"), float("-inf"), float("-inf"))
    summary = _load(Path(summary_path))
    combined = dict(summary.get("combined_holdout") or {})
    trades = int(combined.get("trades") or 0)
    long_share = _safe_float(combined.get("long_share"), default=0.5)
    pf = _safe_float(combined.get("profit_factor"), default=0.0)
    trades_ok = 1.0 if trades >= 50 else 0.0
    side_ok = 1.0 if 0.30 <= long_share <= 0.70 else 0.0
    return (trades_ok, side_ok, pf, float(trades))


def _score_for_deep_hpo(run: Dict[str, Any]) -> tuple[float, ...]:
    """
    Grid C scorer: same as feature_s2 — best PF with constraints.
    """
    return _score_for_feature_s2(run)


_SCORERS = {
    "label_fix": _score_for_label_fix,
    "feature_s2": _score_for_feature_s2,
    "deep_hpo": _score_for_deep_hpo,
}


# ---------------------------------------------------------------------------
# Winner extraction
# ---------------------------------------------------------------------------

def _pick_winner(runs: List[Dict[str, Any]], scorer) -> Optional[Dict[str, Any]]:
    non_failed = [r for r in runs if str(r.get("release_status") or "") != "failed"]
    if not non_failed:
        return None
    return max(non_failed, key=scorer)


def _get_run_dir(run: Dict[str, Any]) -> Optional[Path]:
    rd = run.get("run_dir")
    if not rd:
        return None
    return Path(rd)


def _read_winning_fields(
    run: Dict[str, Any],
) -> Dict[str, Any]:
    """Extract fields needed to configure the next manifest from the winner's resolved_config."""
    run_dir = _get_run_dir(run)
    if run_dir is None:
        raise RuntimeError(f"winner row has no run_dir: {run}")

    resolved_path = run_dir / "resolved_config.json"
    summary_path = run_dir / "summary.json"

    resolved = _load(resolved_path) if resolved_path.exists() else {}
    summary = _load(summary_path) if summary_path.exists() else {}

    windows = dict(resolved.get("windows") or {})
    labels = dict(resolved.get("labels") or {})
    feature_sets = dict((resolved.get("catalog") or {}).get("feature_sets_by_stage") or {})
    stage1_artifacts = dict((summary.get("stage_artifacts") or {}).get("stage1") or {})
    run_name = str(summary.get("run_name") or run.get("grid_run_id") or run_dir.name)

    return {
        "run_dir": str(run_dir.resolve()),
        "run_id": str(run.get("grid_run_id") or run_dir.name),
        "run_name": run_name,
        "windows": windows,
        "stage2_labeler_id": str(labels.get("stage2_labeler_id") or "direction_best_recipe_v1"),
        "stage2_feature_sets": list(feature_sets.get("stage2") or []),
        "stage1_reused_from_run_id": str(stage1_artifacts.get("reused_from_run_id") or ""),
        "stage1_reused_from_run_dir": str(stage1_artifacts.get("reused_from_run_dir") or ""),
    }


# ---------------------------------------------------------------------------
# Manifest patching
# ---------------------------------------------------------------------------

def _patch_for_label_fix_to_feature_s2(
    base: Dict[str, Any],
    winner: Dict[str, Any],
) -> Dict[str, Any]:
    """Update Grid B base manifest after Grid A winner is known."""
    out = dict(base)

    # Update windows to match winner
    if winner["windows"]:
        out["windows"] = winner["windows"]

    # Update S2 labeler
    _deep_set(out, ["labels", "stage2_labeler_id"], winner["stage2_labeler_id"])

    # Set S1 reuse to winner's run
    _deep_set(out, ["training", "stage1_reuse", "source_run_id"], winner["run_name"])
    _deep_set(out, ["training", "stage1_reuse", "source_run_dir"], winner["run_dir"])

    # Remove placeholder comment
    tr = out.get("training", {}).get("stage1_reuse", {})
    tr.pop("_comment", None)

    return out


def _patch_for_feature_s2_to_deep_hpo(
    base: Dict[str, Any],
    winner: Dict[str, Any],
) -> Dict[str, Any]:
    """Update Grid C base manifest after Grid B winner is known."""
    out = dict(base)

    # Update windows to match winner
    if winner["windows"]:
        out["windows"] = winner["windows"]

    # Update S2 labeler
    _deep_set(out, ["labels", "stage2_labeler_id"], winner["stage2_labeler_id"])

    # Update S2 feature set to the winning B run's feature set
    if winner["stage2_feature_sets"]:
        _deep_set(out, ["catalog", "feature_sets_by_stage", "stage2"], winner["stage2_feature_sets"])

    # Remove placeholder feature set comment
    feat_stage2 = (out.get("catalog") or {}).get("feature_sets_by_stage", {}).get("stage2", [])
    if feat_stage2 and isinstance(feat_stage2, list) and any("FILL_IN" in str(f) for f in feat_stage2):
        _deep_set(out, ["catalog", "feature_sets_by_stage", "stage2"], winner["stage2_feature_sets"])

    # Set S1 reuse
    _deep_set(out, ["training", "stage1_reuse", "source_run_id"], winner["run_name"])
    _deep_set(out, ["training", "stage1_reuse", "source_run_dir"], winner["run_dir"])

    tr = out.get("training", {}).get("stage1_reuse", {})
    tr.pop("_comment", None)

    # Remove the windows comment field if present
    out.get("windows", {}).pop("_comment", None)

    return out


_PATCHERS = {
    "label_fix": _patch_for_label_fix_to_feature_s2,
    "feature_s2": _patch_for_feature_s2_to_deep_hpo,
    "deep_hpo": lambda base, _winner: base,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _runs_from_individual_summaries(summary_paths: List[str]) -> List[Dict[str, Any]]:
    """Build pseudo run-rows from individual summary.json files (Grid A mode)."""
    rows = []
    for sp in summary_paths:
        p = Path(sp)
        if not p.exists():
            print(f"WARNING: summary not found, skipping: {p}", file=sys.stderr)
            continue
        summary = _load(p)
        run_dir = str(p.parent.resolve())
        run_id = str(summary.get("run_name") or p.parent.name)
        rows.append({
            "grid_run_id": run_id,
            "run_dir": run_dir,
            "summary_path": str(p.resolve()),
            "release_status": "completed" if summary.get("completion_mode") else "unknown",
        })
    return rows


def _runs_from_grid_summary(grid_summary_path: Path) -> List[Dict[str, Any]]:
    """Extract run rows from a grid_summary.json (Grid B/C mode)."""
    grid_summary = _load(grid_summary_path)
    return list(grid_summary.get("runs") or [])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Update next-grid base manifest from prior run winner.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--grid-summary", help="Path to completed grid_summary.json (Grid B/C mode)")
    src.add_argument("--run-summaries", nargs="+", help="Paths to individual summary.json files (Grid A mode)")
    parser.add_argument("--base-manifest", required=True, help="Path to next grid's base manifest to patch in-place")
    parser.add_argument(
        "--grid-kind",
        required=True,
        choices=list(_SCORERS),
        help="Which grid just completed (drives scoring and patching logic)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print result without writing")
    args = parser.parse_args(argv)

    base_manifest_path = Path(args.base_manifest)
    grid_kind = args.grid_kind

    if not base_manifest_path.exists():
        print(f"ERROR: base manifest not found: {base_manifest_path}", file=sys.stderr)
        return 1

    if args.grid_summary:
        grid_summary_path = Path(args.grid_summary)
        if not grid_summary_path.exists():
            print(f"ERROR: grid summary not found: {grid_summary_path}", file=sys.stderr)
            return 1
        runs = _runs_from_grid_summary(grid_summary_path)
    else:
        runs = _runs_from_individual_summaries(args.run_summaries)

    if not runs:
        print("ERROR: no runs found to pick a winner from", file=sys.stderr)
        return 1

    scorer = _SCORERS[grid_kind]
    winner_row = _pick_winner(runs, scorer)
    if winner_row is None:
        print("ERROR: all runs failed — no winner can be selected", file=sys.stderr)
        return 1

    print(f"Grid kind : {grid_kind}")
    print(f"Winner    : {winner_row.get('grid_run_id')}  (run_dir: {winner_row.get('run_dir')})")
    print(f"Score     : {scorer(winner_row)}")

    try:
        winner_fields = _read_winning_fields(winner_row)
    except Exception as exc:
        print(f"ERROR reading winner fields: {exc}", file=sys.stderr)
        return 1

    print(f"S2 labeler: {winner_fields['stage2_labeler_id']}")
    print(f"S2 feats  : {winner_fields['stage2_feature_sets']}")
    print(f"Windows   : {winner_fields['windows']}")
    print(f"S1 reuse  : {winner_fields['run_name']}  →  {winner_fields['run_dir']}")

    base = _load(base_manifest_path)
    patcher = _PATCHERS[grid_kind]
    patched = patcher(base, winner_fields)

    if args.dry_run:
        print("\n--- DRY RUN: patched manifest (not written) ---")
        print(json.dumps(patched, indent=2))
        return 0

    _dump(base_manifest_path, patched)
    print(f"\nPatched manifest written to: {base_manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
