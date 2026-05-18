"""Run the realistic single-position simulator on all 4 core recipes and report results.

Usage (on GCP ML instance):
    python3 /tmp/run_all_recipes_realistic.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/option_trading")

from ml_pipeline_2.scripts.train_option_pnl_mvp import (
    HOLDOUT_END,
    load_labels_and_features,
    train_recipe,
)

LABELS_ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/option_pnl_labels_v1")
FLAT_ROOT   = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2")
OUT_PATH    = Path("/opt/option_trading/.data/ml_pipeline/all_recipes_realistic_results.json")

RECIPES = ["ATM_CE_9", "ATM_PE_9", "ATM_CE_15", "ATM_PE_15"]

def main():
    results = {}
    for recipe_id in RECIPES:
        print(f"\n=== {recipe_id} ===")
        try:
            df = load_labels_and_features(LABELS_ROOT, FLAT_ROOT, recipe_id)
            print(f"  rows: {len(df)}")
            res = train_recipe(df, recipe_id)
            print(f"  train/valid/holdout: {res.n_train}/{res.n_valid}/{res.n_holdout}")
            print(f"  AUC: {res.holdout_roc_auc:.4f}")
            print(f"  REALISTIC threshold sweep:")
            for s in res.threshold_sweep:
                print(f"    thr={s['threshold']:.2f}  n={s['n_trades']:5d}  net={s['net_pnl_sum']:+8.3f}  wr={s.get('win_rate',0):.3f}")
            print(f"  BEST: thr={res.best_threshold_by_net_pnl:.2f}  net={res.best_holdout_net_pnl_sum:+.3f}  trades={res.best_holdout_trades}  wr={res.best_holdout_win_rate:.3f}")
            print(f"  VERDICT: {'EDGE' if res.best_holdout_net_pnl_sum > 0 else 'NO_EDGE'}")
            from dataclasses import asdict
            results[recipe_id] = asdict(res)
            results[recipe_id]["holdout_verdict"] = "EDGE" if res.best_holdout_net_pnl_sum > 0 else "NO_EDGE"
        except Exception as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            results[recipe_id] = {"error": str(exc)}

    OUT_PATH.write_text(json.dumps(results, indent=2))
    print(f"\n=== Summary ===")
    for rid, r in results.items():
        if "error" in r:
            print(f"  {rid}: ERROR {r['error']}")
        else:
            print(f"  {rid}: {r['holdout_verdict']}  best_thr={r['best_threshold_by_net_pnl']}  net={r['best_holdout_net_pnl_sum']:+.3f}  trades={r['best_holdout_trades']}  wr={r['best_holdout_win_rate']:.3f}")
    print(f"\nResults written to: {OUT_PATH}")

if __name__ == "__main__":
    main()
