"""Extract rows_total/days_total/AUC from research reports + holdout details."""
import glob
import json
import os

HOME = os.path.expanduser("~")


def show(path_glob, label):
    matches = sorted(glob.glob(path_glob))
    if not matches:
        print(f"[{label}] no match")
        return
    run = matches[-1]
    print(f"\n===== {label} =====\n{os.path.basename(run)}")
    for rep in ("training_report.json", "search_report.json"):
        rp = os.path.join(run, "stages", "stage1", rep)
        if not os.path.exists(rp):
            continue
        d = json.load(open(rp))
        print(f"-- {rep}: rows_total={d.get('rows_total')} days_total={d.get('days_total')} "
              f"label_target={d.get('label_target')} objective={d.get('objective')}")
        lb = d.get("leaderboard")
        if isinstance(lb, list):
            for e in lb[:6]:
                if isinstance(e, dict):
                    metrics = {k: e.get(k) for k in e if "auc" in k.lower() or "roc" in k.lower()
                               or k in ("feature_set", "model", "experiment_id", "objective_value",
                                        "holdout_roc_auc", "valid_roc_auc")}
                    print("   lb:", json.dumps(metrics)[:400])
    # Look for any nested roc_auc anywhere
    rp = os.path.join(run, "stages", "stage1", "training_report.json")
    if os.path.exists(rp):
        txt = open(rp).read()
        import re
        hits = re.findall(r'"[^"]*(?:roc_auc|auc)[^"]*"\s*:\s*[0-9.]+', txt)
        for h in hits[:20]:
            print("   AUC-field:", h)


show(os.path.join(HOME, "bmm_run/ml_pipeline_2/artifacts/research/bmm_prod_5m020_v2view_*"), "bmm_prod")
show(os.path.join(HOME, "bmm_run/ml_pipeline_2/artifacts/research/ab_5m020_base_*"), "ab_base")
show(os.path.join(HOME, "bmm_run/ml_pipeline_2/artifacts/research/ab_5m020_bmm_*"), "ab_bmm")
