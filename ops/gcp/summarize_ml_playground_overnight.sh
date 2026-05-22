#!/usr/bin/env bash
# Print a one-screen summary of overnight ML playground artifacts.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/opt/option_trading}"
ART="${REPO_ROOT}/ml_pipeline_2/artifacts/research"

python3 - <<'PY'
import json
from pathlib import Path

art = Path("/opt/option_trading/ml_pipeline_2/artifacts/research")
if not art.exists():
    art = Path("ml_pipeline_2/artifacts/research")

def load_summary(run_dir: Path) -> dict | None:
    p = run_dir / "summary.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())

def best_from_hpo(prefix: str) -> None:
    dirs = sorted(art.glob(f"{prefix}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not dirs:
        print(f"  (no {prefix}_* runs)")
        return
    d = dirs[0]
    s = load_summary(d) or {}
    cv = (s.get("cv_prechecks") or {})
    s1 = cv.get("stage1_cv") or {}
    s2 = cv.get("stage2_cv") or {}
    pa = s.get("publish_assessment") or {}
    stage = (s.get("stage_artifacts") or {})
    print(f"  run_id: {s.get('run_id', d.name)}")
    print(f"  status: {s.get('status')} completion: {s.get('completion_mode')}")
    print(f"  publish: {pa.get('decision')} blocking={pa.get('blocking_reasons')}")
    if s1:
        print(f"  stage1_cv: auc={s1.get('roc_auc')} drift={s1.get('roc_auc_drift_half_split')} gate={s1.get('gate_passed')}")
    if s2:
        print(f"  stage2_cv: auc={s2.get('roc_auc')} drift={s2.get('roc_auc_drift_half_split')} gate={s2.get('gate_passed')}")
    tr = (stage.get("stage1") or stage.get("stage2") or {})
    if tr.get("training_report_path"):
        trp = Path(tr["training_report_path"])
        if trp.exists():
            rep = json.loads(trp.read_text())
            best = rep.get("best_experiment") or {}
            print(f"  best: {best.get('experiment_id')} features={best.get('feature_count')} obj={best.get('objective_value')}")

def grid_top(grid_name: str) -> None:
    dirs = sorted(art.glob(f"{grid_name}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not dirs:
        print(f"  (no {grid_name}_* grid)")
        return
    d = dirs[0]
    gs = d / "grid_summary.json"
    if not gs.exists():
        print(f"  grid dir {d.name} (no grid_summary.json yet)")
        return
    g = json.loads(gs.read_text())
    print(f"  grid: {d.name} status={g.get('status')}")
    rows = g.get("ranked_runs") or g.get("runs") or []
    for row in rows[:5]:
        rid = row.get("run_id") or row.get("lane_run_id")
        pub = row.get("publishable")
        s2 = (row.get("stage2_cv") or {})
        comb = (row.get("combined_holdout_summary") or {})
        print(
            f"    {rid}: publishable={pub} "
            f"s2_auc={s2.get('roc_auc')} pf={comb.get('profit_factor')} trades={comb.get('trades')}"
        )

print("=== Entry HPO (latest) ===")
best_from_hpo("entry_s1_only_hpo_v2")
if not list(art.glob("entry_s1_only_hpo_v2_*")):
    best_from_hpo("entry_s1_only_hpo_v1")

print("\n=== Direction HPO (latest) ===")
best_from_hpo("direction_s2_only_hpo_v2")
if not list(art.glob("direction_s2_only_hpo_v2_*")):
    best_from_hpo("direction_s2_only_hpo_v1")

print("\n=== Entry feature grid (latest) ===")
grid_top("staged_grid_entry_playground_v1")

print("\n=== Direction feature grid (latest) ===")
grid_top("staged_grid_direction_playground_v1")
PY
