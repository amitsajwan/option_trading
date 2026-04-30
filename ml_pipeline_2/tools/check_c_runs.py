"""Show detailed metrics for Grid C runs."""
import json
from pathlib import Path

BASE = Path("/home/savitasajwan03/option_trading/ml_pipeline_2/artifacts/research")

for prefix in ["staged_deep_hpo_c1_base", "staged_deep_hpo_c2_long_train", "staged_deep_hpo_c3_long_valid",
               "staged_deep_hpo_d1_zero_cost", "staged_deep_hpo_d2_high_edge"]:
    matches = sorted(BASE.glob(f"{prefix}*"), reverse=True)
    if not matches:
        print(f"[not found] {prefix}")
        continue
    run_dir = matches[0]
    sp = run_dir / "summary.json"
    if not sp.exists():
        print(f"[no summary] {run_dir.name}")
        continue
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
    ce_trades = sum(float(v.get("trades") or 0) * float(v.get("long_share") or 0) for v in segments.values())
    long_share = ce_trades / total_trades if total_trades > 0 else None
    pa = dict(s.get("publish_assessment") or {})
    blocking = pa.get("blocking_reasons") or []

    roc_str = f"{s2_roc:.4f}" if s2_roc is not None else "N/A"
    ls_str = f"{long_share:.2f}" if long_share is not None else "N/A"
    print(f"\n{'='*60}")
    print(f"{run_dir.name}")
    print(f"  mode={mode}  s2_roc={roc_str}  trades={total_trades}  net={total_net:.4f}  long={ls_str}")
    print(f"  blocking: {blocking}")
    for rname, rv in segments.items():
        t = int(rv.get("trades") or 0)
        pf_val = rv.get("profit_factor")
        pf_str = f"{float(pf_val):.2f}" if pf_val is not None else "N/A"
        ls_val = rv.get("long_share")
        ls2_str = f"{float(ls_val):.2f}" if ls_val is not None else "N/A"
        nr_val = rv.get("net_return_sum")
        nr_str = f"{float(nr_val):.4f}" if nr_val is not None else "N/A"
        print(f"    {rname:<14} trades={t:4d}  PF={pf_str}  long={ls2_str}  net={nr_str}")
