#!/usr/bin/env python3
"""Summarize the 5 entry stage-1 ablation runs into a single comparison table.

Run on VM after experiments complete:
    python3 ops/gcp/summarize_entry_ablations.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIGS = [
    ("E1", "entry_s1_ablate_e1_c1_repro", "pure C1 reproduction"),
    ("E2", "entry_s1_ablate_e2_view_v2", "C1 + view v2"),
    ("E3", "entry_s1_ablate_e3_velocity", "C1 + fo_velocity_v1"),
    ("E4", "entry_s1_ablate_e4_harsh_label", "C1 + harsh 100pts labeler"),
    ("E5", "entry_s1_ablate_e5_short_window", "C1 + 2022-2024 only"),
]

ROOT = Path("/opt/option_trading/ml_pipeline_2/artifacts/research")


def latest_run(name: str) -> Path | None:
    matches = sorted(ROOT.glob(f"{name}_*"))
    return matches[-1] if matches else None


def load_summary(run_dir: Path) -> dict | None:
    p = run_dir / "summary.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def fmt(v, w=8, prec=3):
    if v is None:
        return "n/a".rjust(w)
    if isinstance(v, float):
        return f"{v:>{w}.{prec}f}"
    return f"{str(v):>{w}}"


def main() -> int:
    print(f"{'ID':3} {'run_name':36} {'status':18} {'cv_auc':>8} {'drift':>7} "
          f"{'holdout_auc':>12} {'val_PF':>8} {'val_trades':>10} {'maxprob':>8} {'thr_sel':>8}")
    print("-" * 130)

    for eid, name, label in CONFIGS:
        run_dir = latest_run(name)
        if run_dir is None:
            print(f"{eid:3} {name:36} {'(no run found)':18}")
            continue
        s = load_summary(run_dir)
        if s is None:
            print(f"{eid:3} {name:36} {'(no summary.json)':18}")
            continue

        status = s.get("completion_mode") or s.get("status") or "?"

        # stage1 CV + holdout
        cv = None
        drift = None
        try:
            sa = s.get("stage_artifacts", {}).get("stage1", {})
            cv_summary = sa.get("cv_summary", {}) or {}
            cv = cv_summary.get("roc_auc") or cv_summary.get("mean_roc_auc")
            drift = cv_summary.get("roc_auc_drift_half_split")
        except Exception:
            pass

        hr = s.get("holdout_reports", {}).get("stage1", {}) or {}
        holdout_auc = hr.get("roc_auc")

        # selected threshold & validation PF
        pr = s.get("policy_reports", {}).get("stage1", {}) or {}
        sel = pr.get("selected_validation_summary") or {}
        val_pf = sel.get("profit_factor")
        val_trades = sel.get("trades")
        thr_sel = pr.get("selected_threshold")

        # holdout score distribution
        scoring = s.get("scenario_reports", {}).get("stage1_holdout_score_distribution", {}) or {}
        maxprob = scoring.get("max") or scoring.get("p99") or scoring.get("max_prob")

        print(f"{eid:3} {name:36} {status:18} "
              f"{fmt(cv)} {fmt(drift)} {fmt(holdout_auc, 12)} "
              f"{fmt(val_pf)} {fmt(val_trades, 10, 0)} {fmt(maxprob)} {fmt(thr_sel)}")

    print()
    print("Reference: C1 published baseline — stage1 holdout AUC=0.683, drift=0.017,")
    print("           validation PF=3.99 @ thr=0.50 with ~22K trades.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
