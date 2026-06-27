"""Inspect trained entry bundles: feature contracts, holdout AUC, row counts."""
import glob
import json
import os
import sys

import joblib

HOME = os.path.expanduser("~")


def show_research(path_glob, label):
    matches = sorted(glob.glob(path_glob))
    if not matches:
        print(f"[{label}] no match for {path_glob}")
        return
    run = matches[-1]
    print(f"\n===== {label} =====\n{run}")
    mj = os.path.join(run, "stages", "stage1", "model.joblib")
    if os.path.exists(mj):
        b = joblib.load(mj)
        print("bundle_top_keys=", list(b.keys()) if isinstance(b, dict) else type(b))
        contract = b.get("_model_input_contract") if isinstance(b, dict) else None
        feats = None
        if isinstance(contract, dict):
            feats = contract.get("feature_columns") or contract.get("features")
        if feats is None and isinstance(b, dict):
            feats = b.get("feature_columns") or b.get("features")
        if feats is not None:
            print(f"n_features={len(feats)}")
            print("features=", list(feats))
    for rep in ("training_report.json", "search_report.json"):
        rp = os.path.join(run, "stages", "stage1", rep)
        if os.path.exists(rp):
            data = json.load(open(rp))
            txt = json.dumps(data)
            # print holdout/roc snippets
            print(f"-- {rep} keys=", list(data.keys()))
            for k in ("holdout", "holdout_scores", "roc_auc", "metrics", "training_report", "best_experiment"):
                if k in data:
                    print(f"   {k}: {json.dumps(data[k])[:600]}")


show_research(os.path.join(HOME, "bmm_run/ml_pipeline_2/artifacts/research/bmm_prod_5m020_v2view_*"), "bmm_prod")
show_research(os.path.join(HOME, "bmm_run/ml_pipeline_2/artifacts/research/ab_5m020_base_*"), "ab_base (velocity)")
show_research(os.path.join(HOME, "bmm_run/ml_pipeline_2/artifacts/research/ab_5m020_bmm_*"), "ab_bmm (compression)")
