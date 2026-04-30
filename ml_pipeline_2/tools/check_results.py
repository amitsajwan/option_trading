"""Quick result checker — run on the VM to print Grid A/B/C metrics."""
import json
from pathlib import Path

BASE = Path("/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research")
CONFIGS = Path("/home/savitasajwan03/option_trading/ml_pipeline_2/configs/research")

A_RUNS = [
    ("A1 window_shift",      "staged_label_fix_a1_window_shift"),
    ("A2 market_direction",  "staged_label_fix_a2_market_direction"),
    ("A3 combined",          "staged_label_fix_a3_combined"),
]

B_GRID_GLOB = "staged_grid_feature_s2_v1_*"
C_GRID_GLOB = "staged_grid_deep_hpo_v1_*"


def fmt_run(summary: dict) -> str:
    ch = summary.get("combined_holdout") or {}
    sq = (summary.get("stage_quality") or {}).get("stage2") or {}
    roc   = sq.get("roc_auc")
    tr    = ch.get("trades")
    pf    = ch.get("profit_factor")
    ls    = ch.get("long_share")
    nr    = ch.get("net_return_sum")
    roc_s = f"{roc:.3f}" if roc is not None else "N/A"
    pf_s  = f"{pf:.3f}"  if pf  is not None else "N/A"
    ls_s  = f"{ls:.1%}"  if ls  is not None else "N/A"
    nr_s  = f"{nr:.4f}"  if nr  is not None else "N/A"
    return f"  S2_ROC={roc_s}  trades={tr}  PF={pf_s}  long_share={ls_s}  net_return={nr_s}"


print("=" * 70)
print("GRID A — Individual runs")
print("=" * 70)
for label, dirname in A_RUNS:
    sp = BASE / dirname / "summary.json"
    if sp.exists():
        s = json.loads(sp.read_text())
        gates = s.get("gates") or {}
        passed = gates.get("combined_passed", "?")
        print(f"{label}  [combined_passed={passed}]")
        print(fmt_run(s))
    else:
        print(f"{label}  — no summary.json yet")

print()
print("=" * 70)
print("GRID B — Feature set grid")
print("=" * 70)
grid_dirs = sorted(BASE.glob(B_GRID_GLOB), reverse=True)
if not grid_dirs:
    print("  No Grid B output dir found yet")
else:
    gdir = grid_dirs[0]
    gs = gdir / "grid_summary.json"
    if gs.exists():
        data = json.loads(gs.read_text())
        print(f"Status : {data.get('status')}")
        for row in data.get("runs") or []:
            rid  = row.get("grid_run_id")
            rstatus = row.get("release_status")
            rank = row.get("rank", "?")
            sp = row.get("summary_path")
            if sp and Path(sp).exists():
                s = json.loads(Path(sp).read_text())
                print(f"  rank={rank} {rid} [{rstatus}]")
                print(fmt_run(s))
            else:
                print(f"  {rid} [{rstatus}] — no summary yet")
        winner = data.get("winner") or {}
        print(f"\nWinner: {winner.get('grid_run_id')}")
    else:
        gs_status = gdir / "grid_status.json"
        if gs_status.exists():
            st = json.loads(gs_status.read_text())
            print(f"  Grid B still running. Status: {st.get('lifecycle_status')}  dir: {gdir.name}")
            runs_root = gdir / "runs"
            if runs_root.exists():
                for rd in sorted(runs_root.iterdir()):
                    sp = rd / "summary.json"
                    st2 = rd / "run_status.json"
                    if sp.exists():
                        s = json.loads(sp.read_text())
                        print(f"  [done] {rd.name}")
                        print(fmt_run(s))
                    elif st2.exists():
                        s2 = json.loads(st2.read_text())
                        print(f"  [running] {rd.name}  status={s2.get('lifecycle_status')}")
                    else:
                        print(f"  [pending] {rd.name}")
        else:
            print(f"  Grid B started but no status file yet — dir: {gdir.name}")

print()
print("=" * 70)
print("GRID C — Deep HPO")
print("=" * 70)
c_dirs = sorted(BASE.glob(C_GRID_GLOB), reverse=True)
if not c_dirs:
    print("  Not started yet")
else:
    print(f"  Dir: {c_dirs[0].name}")
    gs = c_dirs[0] / "grid_summary.json"
    print(f"  grid_summary.json exists: {gs.exists()}")
