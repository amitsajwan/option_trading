import json
from pathlib import Path

BASE = Path("/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research")

A_RUNS = [
    ("A1", "staged_label_fix_a1_window_shift"),
    ("A2", "staged_label_fix_a2_market_direction"),
    ("A3", "staged_label_fix_a3_combined"),
]

print("=" * 70)
print("GRID A — Detailed diagnostics")
print("=" * 70)
for label, dirname in A_RUNS:
    sp = BASE / dirname / "summary.json"
    if not sp.exists():
        print(f"{label}: no summary.json"); continue
    s = json.loads(sp.read_text())
    print(f"\n{label} [{dirname}]")
    print(f"  status          : {s.get('status')}")
    print(f"  completion_mode : {s.get('completion_mode')}")

    # CV prechecks
    cv = s.get("cv_prechecks") or {}
    if isinstance(cv, dict):
        for stage, info in cv.items():
            if isinstance(info, dict):
                print(f"  cv_{stage}: roc_auc={info.get('roc_auc')}  passed={info.get('passed')}  reason={info.get('reason','')}")

    # training_environment — stage2 CV
    te = s.get("training_environment") or {}
    s2te = te.get("stage2") or {}
    cv2 = s2te.get("cv") or {}
    print(f"  s2_cv_roc       : {cv2.get('roc_auc_mean')}  passed={cv2.get('signal_check_passed')}")

    # scenario_reports — holdout metrics if available
    sr = s.get("scenario_reports") or {}
    combined = sr.get("combined_holdout") or {}
    print(f"  trades          : {combined.get('trades')}  PF={combined.get('profit_factor')}  long_share={combined.get('long_share')}")

    # publish assessment
    pa = s.get("publish_assessment") or {}
    print(f"  publishable     : {pa.get('publishable')}  blocking={pa.get('blocking_reasons')}")

print()
print("=" * 70)
print("GRID B — Run details")
print("=" * 70)
grid_dirs = sorted(BASE.glob("staged_grid_feature_s2_v1_*"), reverse=True)
if not grid_dirs:
    print("No Grid B dir"); exit()
gdir = grid_dirs[0]
runs_root = gdir / "runs"
if not runs_root.exists():
    print("No runs subdir yet"); exit()
for rd in sorted(runs_root.iterdir()):
    sp2 = rd / "summary.json"
    if sp2.exists():
        s2 = json.loads(sp2.read_text())
        te2 = s2.get("training_environment") or {}
        s2te2 = te2.get("stage2") or {}
        cv2 = s2te2.get("cv") or {}
        sr2 = s2.get("scenario_reports") or {}
        combined2 = sr2.get("combined_holdout") or {}
        print(f"  [done] {rd.name}")
        print(f"    completion={s2.get('completion_mode')}  s2_cv_roc={cv2.get('roc_auc_mean')}  trades={combined2.get('trades')}  PF={combined2.get('profit_factor')}  long_share={combined2.get('long_share')}")
    else:
        st = rd / "run_status.json"
        status = json.loads(st.read_text()).get("lifecycle_status") if st.exists() else "unknown"
        print(f"  [{'running' if status != 'completed' else 'done'}] {rd.name}  status={status}")
