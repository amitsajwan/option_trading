"""Portfolio backtest combining multiple option-P&L recipe predictions.

Real deployment isn't "one recipe always" — it's "pick the recipe most
likely to fire today, or stack multiple, or sit out if none clear cost."
This script simulates a few simple portfolio policies on the existing
holdout and reports per-policy net P&L + per-day trade count.

Policies:
  - 'union_above_threshold': trade whenever ANY recipe's prob >= threshold
    (operator stacks all 3 PE recipes; risk warning if same-minute conflict)
  - 'best_recipe_per_snapshot': for each snapshot pick the recipe with
    highest prob, trade if its prob >= threshold
  - 'min_one_above': trade ONLY if at least N recipes agree

Holdout window: same C1 holdout (2024-08 → 2024-10) — single-window so
treat with same caveats as MVP. Walk-forward portfolio is a future v2.

Usage:
    python -m ml_pipeline_2.scripts.portfolio_backtest_option_pnl \\
      --recipes ATM_PE_15 ATM_PE_9 OTM1_PE_15 \\
      --labels-roots <v1> <v1> <grid> \\
      --params-jsons <hpo15> <none> <none> \\
      --out <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

try:
    import xgboost as xgb
    from sklearn.metrics import roc_auc_score
except ImportError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(2)

from ml_pipeline_2.scripts.train_option_pnl_mvp import (
    DEFAULT_FLAT_ROOT, HOLDOUT_END, TRAIN_END, VALID_END,
    load_labels_and_features, select_feature_columns, split_temporal,
)

THRESHOLDS = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
DEFAULT_PARAMS = dict(
    n_estimators=300, max_depth=4, learning_rate=0.05,
    subsample=0.85, colsample_bytree=0.85, reg_lambda=2.0,
)


def train_and_score(df: pd.DataFrame, params: Optional[dict] = None):
    """Fit on train, return (holdout DataFrame copy with predicted_prob column)."""
    feat_cols = select_feature_columns(df)
    train, _, holdout = split_temporal(df)
    X_tr = train[feat_cols].fillna(0.0).to_numpy(dtype=np.float32)
    y_tr = train["label"].astype(int).to_numpy()
    X_ho = holdout[feat_cols].fillna(0.0).to_numpy(dtype=np.float32)
    y_ho = holdout["label"].astype(int).to_numpy()

    model_params = {**DEFAULT_PARAMS, **(params or {})}
    model = xgb.XGBClassifier(
        **model_params, objective="binary:logistic", eval_metric="auc",
        tree_method="hist", n_jobs=4, random_state=42,
    )
    model.fit(X_tr, y_tr, verbose=False)
    p_ho = model.predict_proba(X_ho)[:, 1]
    out = holdout[["trade_date", "snapshot_id", "label", "net_pnl_pct"]].copy()
    out["predicted_prob"] = p_ho
    try:
        out_auc = float(roc_auc_score(y_ho, p_ho))
    except Exception:
        out_auc = float("nan")
    return out, out_auc


def run(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if len(args.recipes) != len(args.labels_roots):
        print("ERROR: --recipes and --labels-roots must have same length", file=sys.stderr)
        return 2
    if args.params_jsons and len(args.params_jsons) != len(args.recipes):
        print("ERROR: --params-jsons must match --recipes length (use 'none' for no override)", file=sys.stderr)
        return 2

    print(f"=== Portfolio backtest ===")
    print(f"recipes: {args.recipes}")
    print(f"holdout: {VALID_END.date()} → {HOLDOUT_END.date()}")

    # Score each recipe on holdout
    per_recipe: dict[str, pd.DataFrame] = {}
    aucs: dict[str, float] = {}
    for i, rid in enumerate(args.recipes):
        labels_root = Path(args.labels_roots[i])
        params = None
        if args.params_jsons:
            pj = args.params_jsons[i]
            if pj and pj.lower() != "none":
                params = json.loads(Path(pj).read_text())
                if isinstance(params, dict) and "trials" in params:
                    params = params["trials"][0]["params"]
        print(f"\n[{rid}] training (params={'HPO' if params else 'default'})...")
        df = load_labels_and_features(labels_root, Path(args.flat), rid)
        scored, auc = train_and_score(df, params=params)
        scored = scored.rename(columns={
            "label": f"label_{rid}",
            "net_pnl_pct": f"pnl_{rid}",
            "predicted_prob": f"prob_{rid}",
        })
        per_recipe[rid] = scored
        aucs[rid] = auc
        print(f"  AUC: {auc:.4f}  rows: {len(scored)}")

    # Join on (trade_date, snapshot_id). Use outer join so a recipe that
    # skipped a snapshot doesn't drop it from the portfolio analysis.
    base_rid = args.recipes[0]
    merged = per_recipe[base_rid]
    for rid in args.recipes[1:]:
        merged = merged.merge(
            per_recipe[rid],
            on=["trade_date", "snapshot_id"], how="outer",
        )
    print(f"\nmerged holdout rows: {len(merged)}")

    # Policy A — union above threshold: at each row, if ANY recipe's prob >= thr,
    # take that recipe's trade (if multiple cross, pick the highest-prob one).
    print("\n--- Policy A: 'union above threshold' (highest-prob recipe wins ties) ---")
    sweep_A: list[dict] = []
    for thr in THRESHOLDS:
        prob_cols = [f"prob_{rid}" for rid in args.recipes]
        pnl_cols = [f"pnl_{rid}" for rid in args.recipes]
        probs = merged[prob_cols].fillna(-1.0).to_numpy()
        pnls = merged[pnl_cols].fillna(np.nan).to_numpy()
        max_prob = probs.max(axis=1)
        best_idx = probs.argmax(axis=1)
        mask = max_prob >= thr
        chosen_pnl = pnls[np.arange(len(merged)), best_idx]
        valid = mask & np.isfinite(chosen_pnl)
        n_trades = int(valid.sum())
        if n_trades == 0:
            sweep_A.append({"threshold": thr, "n_trades": 0, "net_pnl": 0.0, "win_rate": 0.0})
            continue
        traded = chosen_pnl[valid]
        sweep_A.append({
            "threshold": thr, "n_trades": n_trades,
            "net_pnl": float(traded.sum()),
            "win_rate": float((traded > 0).mean()),
        })
    for s in sweep_A:
        print(f"  thr={s['threshold']:.2f}  n_trades={s['n_trades']:5d}  net={s['net_pnl']:+8.2f}  wr={s.get('win_rate',0):.3f}")
    best_A = max(sweep_A, key=lambda d: d["net_pnl"])
    print(f"  BEST A: thr={best_A['threshold']:.2f}  n_trades={best_A['n_trades']}  net={best_A['net_pnl']:+.2f}")

    # Policy B — independent per-recipe trading (operator could do this with 1 lot per recipe)
    print("\n--- Policy B: 'independent per-recipe' (sum of each recipe's own threshold-best) ---")
    per_recipe_best = {}
    for rid in args.recipes:
        prob_col = f"prob_{rid}"
        pnl_col = f"pnl_{rid}"
        # Find best threshold for this recipe on its own
        rid_best = {"net_pnl": -1e9}
        for thr in THRESHOLDS:
            sub = merged[[prob_col, pnl_col]].dropna()
            mask = sub[prob_col] >= thr
            if mask.sum() == 0:
                continue
            traded = sub.loc[mask, pnl_col]
            net = float(traded.sum())
            if net > rid_best["net_pnl"]:
                rid_best = {"threshold": thr, "n_trades": int(mask.sum()), "net_pnl": net,
                            "win_rate": float((traded > 0).mean())}
        per_recipe_best[rid] = rid_best
        print(f"  {rid}: thr={rid_best.get('threshold')}  n_trades={rid_best.get('n_trades')}  "
              f"net={rid_best.get('net_pnl'):+.2f}  wr={rid_best.get('win_rate', 0):.3f}")
    total_B = sum(r["net_pnl"] for r in per_recipe_best.values())
    print(f"  TOTAL B: {total_B:+.2f}")

    summary = {
        "recipes": args.recipes,
        "aucs": aucs,
        "policy_A_sweep": sweep_A,
        "policy_A_best": best_A,
        "policy_B_per_recipe_best": per_recipe_best,
        "policy_B_total": total_B,
    }
    (out_dir / "portfolio_results.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nwrote: {out_dir / 'portfolio_results.json'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Portfolio backtest combining option-P&L recipes")
    p.add_argument("--recipes", nargs="+", required=True)
    p.add_argument("--labels-roots", nargs="+", required=True,
                   help="One labels root per recipe (parallel arrays)")
    p.add_argument("--params-jsons", nargs="*", default=None,
                   help="One params json (or 'none') per recipe")
    p.add_argument("--flat", default=str(DEFAULT_FLAT_ROOT))
    p.add_argument("--out", required=True)
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
