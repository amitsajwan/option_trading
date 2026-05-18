"""Multi-bundle realistic replay.

Loads two published bundles (PE_9 + CE_9), scores every holdout bar through
the same select_best_bundle_decision logic the runtime uses, applies
single-position blocking (no concurrent entries), and reports net P&L.

Usage (on GCP ML instance):
    python3 /tmp/run_multi_bundle_replay.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/opt/option_trading")

import joblib
import numpy as np
import pandas as pd

from ml_pipeline_2.scripts.train_option_pnl_mvp import (
    HOLDOUT_END,
    VALID_END,
    load_labels_and_features,
    select_feature_columns,
    split_temporal,
)

HOLDOUT_START = VALID_END + pd.Timedelta(days=1)

PE9_BUNDLE  = Path("/opt/option_trading/.data/ml_pipeline/option_pnl_published_models/option_pnl_atm_pe_9_20260518_063221/option_pnl_atm_pe_9_20260518_063304")
CE9_BUNDLE  = Path("/opt/option_trading/.data/ml_pipeline/option_pnl_published_models/option_pnl_atm_ce_9_20260518_063305/option_pnl_atm_ce_9_20260518_063335")
THRESHOLDS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]
LABELS_ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/option_pnl_labels_v1")
FLAT_ROOT   = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2")
OUT_PATH    = Path("/opt/option_trading/.data/ml_pipeline/multi_bundle_replay_results.json")


def load_bundle(bundle_dir: Path) -> dict[str, Any]:
    meta     = json.loads((bundle_dir / "metadata.json").read_text())
    feat_cols = json.loads((bundle_dir / "feature_columns.json").read_text())["feature_columns"]
    model    = joblib.load(bundle_dir / "model.joblib")
    return {
        "recipe_id":  meta["recipe_id"],
        "option_type": meta["recipe_params"]["option_type"],
        "threshold":  float(meta["decision_threshold"]),
        "max_hold_bars": int(meta["recipe_params"]["max_hold_bars"]),
        "feature_columns": feat_cols,
        "model": model,
    }


def score_bundle(bundle: dict, row: pd.Series) -> float:
    x = np.array([float(row.get(c, 0.0) or 0.0) for c in bundle["feature_columns"]], dtype=np.float32).reshape(1, -1)
    return float(bundle["model"].predict_proba(x)[0, 1])


def simulate_multi_bundle(
    holdout_pe: pd.DataFrame,
    holdout_ce: pd.DataFrame,
    bundle_pe: dict,
    bundle_ce: dict,
) -> dict[str, Any]:
    """Merge PE and CE holdout rows into a single sorted timeline, score both
    bundles per bar, select highest margin above threshold (mirrors
    select_best_bundle_decision), apply single-position blocking, accumulate P&L.

    Each row in the merged timeline represents one bar. For bars where both PE
    and CE rows exist (same timestamp), we score both and pick the best.
    Single-position blocking: once a trade fires, skip all subsequent bars
    until the label's natural exit (net_pnl_pct is the realised P&L).
    """
    # Tag each df with its bundle type then merge into one timeline
    pe_tagged = holdout_pe.copy()
    pe_tagged["_bundle_type"] = "PE"
    ce_tagged = holdout_ce.copy()
    ce_tagged["_bundle_type"] = "CE"

    # Use timestamp as the sort key; trade_date is date-only so less granular
    ts_col = "timestamp" if "timestamp" in holdout_pe.columns else "trade_date"
    merged = pd.concat([pe_tagged, ce_tagged], ignore_index=True)
    merged = merged.sort_values([ts_col, "_bundle_type"]).reset_index(drop=True)

    trades: list[dict] = []
    position_open = False
    bars_remaining = 0
    i = 0
    while i < len(merged):
        # Collect all rows at this exact timestamp (could be PE + CE for same bar)
        ts_val = merged.at[i, ts_col]
        bar_rows = []
        while i < len(merged) and merged.at[i, ts_col] == ts_val:
            bar_rows.append(merged.iloc[i])
            i += 1

        if position_open:
            bars_remaining -= 1
            if bars_remaining <= 0:
                position_open = False
            continue

        # Score all bundle candidates at this bar; pick best margin
        best_margin = -1.0
        best_bundle = None
        best_prob   = 0.0
        best_pnl    = 0.0

        for row in bar_rows:
            btype = row["_bundle_type"]
            bundle = bundle_pe if btype == "PE" else bundle_ce
            prob = score_bundle(bundle, row)
            margin = prob - bundle["threshold"]
            if margin > best_margin:
                best_margin = margin
                best_bundle = bundle
                best_prob   = prob
                best_pnl    = float(row.get("net_pnl_pct", 0.0) or 0.0)

        if best_bundle is None or best_margin < 0:
            continue  # HOLD — no bundle cleared threshold

        trades.append({
            "ts":         str(ts_val),
            "recipe_id":  best_bundle["recipe_id"],
            "option_type": best_bundle["option_type"],
            "prob":       best_prob,
            "margin":     best_margin,
            "pnl":        best_pnl,
        })
        position_open  = True
        bars_remaining = int(best_bundle["max_hold_bars"])

    if not trades:
        return {"n_trades": 0, "net_pnl": 0.0, "win_rate": 0.0, "avg_prob": 0.0,
                "avg_margin": 0.0, "trades_by_type": {}}

    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    pe_trades = [t for t in trades if t["option_type"] == "PE"]
    ce_trades = [t for t in trades if t["option_type"] == "CE"]

    return {
        "n_trades":   len(trades),
        "net_pnl":    round(sum(pnls), 4),
        "win_rate":   round(wins / len(trades), 4),
        "avg_prob":   round(float(np.mean([t["prob"] for t in trades])), 4),
        "avg_margin": round(float(np.mean([t["margin"] for t in trades])), 4),
        "trades_by_type": {
            "PE": {"n": len(pe_trades), "net_pnl": round(sum(t["pnl"] for t in pe_trades), 4)},
            "CE": {"n": len(ce_trades), "net_pnl": round(sum(t["pnl"] for t in ce_trades), 4)},
        },
    }


def main():
    print("=== Multi-bundle replay: ATM_PE_9 + ATM_CE_9 ===")
    print(f"Holdout window: {HOLDOUT_START} → {HOLDOUT_END}")
    print()

    print("Loading bundles...")
    bundle_pe = load_bundle(PE9_BUNDLE)
    bundle_ce = load_bundle(CE9_BUNDLE)
    print(f"  PE9: recipe={bundle_pe['recipe_id']} max_hold={bundle_pe['max_hold_bars']}")
    print(f"  CE9: recipe={bundle_ce['recipe_id']} max_hold={bundle_ce['max_hold_bars']}")
    print()

    print("Loading holdout data...")
    df_pe = load_labels_and_features(LABELS_ROOT, FLAT_ROOT, "ATM_PE_9")
    df_ce = load_labels_and_features(LABELS_ROOT, FLAT_ROOT, "ATM_CE_9")
    _, _, holdout_pe = split_temporal(df_pe)
    _, _, holdout_ce = split_temporal(df_ce)
    print(f"  PE9 holdout rows: {len(holdout_pe)}")
    print(f"  CE9 holdout rows: {len(holdout_ce)}")
    print()

    print("=== Threshold sweep (same thr for both bundles) ===")
    sweep_results = []
    for thr in THRESHOLDS:
        bundle_pe["threshold"] = thr
        bundle_ce["threshold"] = thr
        r = simulate_multi_bundle(holdout_pe, holdout_ce, bundle_pe, bundle_ce)
        sweep_results.append({"threshold": thr, **r})
        by_type = r["trades_by_type"]
        pe_info = f"PE:{by_type.get('PE',{}).get('n',0)}" if by_type else ""
        ce_info = f"CE:{by_type.get('CE',{}).get('n',0)}" if by_type else ""
        print(f"  thr={thr:.2f}  trades={r['n_trades']:5d}  net={r['net_pnl']:+8.4f}  wr={r['win_rate']:.3f}  [{pe_info} {ce_info}]")

    best = max(sweep_results, key=lambda x: x["net_pnl"])
    print()
    print(f"=== Best ===")
    print(f"  threshold    : {best['threshold']}")
    print(f"  Total trades : {best['n_trades']}")
    print(f"  Net P&L      : {best['net_pnl']:+.4f}")
    print(f"  Win rate     : {best['win_rate']:.3f}")
    print(f"  By type:")
    for t, v in best["trades_by_type"].items():
        print(f"    {t}: {v['n']} trades  net={v['net_pnl']:+.4f}")

    output = {"sweep": sweep_results, "best": best}
    OUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"\nResults written to: {OUT_PATH}")


if __name__ == "__main__":
    main()
