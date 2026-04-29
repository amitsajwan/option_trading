"""Show detailed metrics for completed Grid B runs."""
import json
from pathlib import Path

BASE = Path("/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research")
grid_dirs = sorted(BASE.glob("staged_grid_feature_s2_v1_*"), reverse=True)

# Use the most recent (correct) Grid B dir
for gdir in grid_dirs:
    runs_root = gdir / "runs"
    if not runs_root.exists():
        continue
    print(f"Grid dir: {gdir.name}")
    for rd in sorted(runs_root.iterdir()):
        sp = rd / "summary.json"
        if not sp.exists():
            print(f"  [not done] {rd.name}"); continue
        s = json.loads(sp.read_text())
        mode = s.get("completion_mode", "?")
        cv = dict(s.get("cv_prechecks") or {})
        s2cv = dict(cv.get("stage2_cv") or {})
        s2_roc = s2cv.get("roc_auc")
        sr = dict(s.get("scenario_reports") or {})
        regime = dict(sr.get("regime") or {})
        segments = dict(regime.get("segments") or {})
        total_trades = sum(int(v.get("trades") or 0) for v in segments.values())
        total_net = sum(float(v.get("net_return_sum") or 0.0) for v in segments.values())
        ce_trades = sum(float(v.get("trades") or 0) * float(v.get("long_share") or 0)
                        for v in segments.values())
        long_share = ce_trades / total_trades if total_trades > 0 else None
        pa = dict(s.get("publish_assessment") or {})
        blocking = pa.get("blocking_reasons") or []
        print(f"  {rd.name}")
        roc_str = f"{s2_roc:.4f}" if s2_roc is not None else "N/A"
        ls_str = f"{long_share:.2f}" if long_share is not None else "N/A"
        print(f"    mode={mode}  s2_roc={roc_str}  trades={total_trades}  net={total_net:.4f}  long_share={ls_str}")
        print(f"    blocking: {blocking}")
        for rname, rv in segments.items():
            t = int(rv.get("trades") or 0)
            pf = float(rv.get("profit_factor") or 0.0)
            ls = float(rv.get("long_share") or 0.0)
            nr = float(rv.get("net_return_sum") or 0.0)
            print(f"      {rname:<12} trades={t:3d}  PF={pf:.2f}  long={ls:.2f}  net={nr:.4f}")
    break  # only most recent
