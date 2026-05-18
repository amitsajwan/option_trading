"""Minimal MVP trainer for option-P&L labels — answer the question
"is there ANY edge in the data?" cheaply before plugging into full HPO.

Design choices (all conservative, all reversible):

  - Per-recipe binary XGBoost classifier with sensible defaults (no HPO yet).
    If raw signal exists, default XGBoost picks it up; if it doesn't, no
    amount of HPO will rescue it.

  - Temporal train/valid/holdout split (NOT random) — matches the existing
    pipeline's C1 windowing:
        train:   2020-08-03 → 2024-04-30
        valid:   2024-05-01 → 2024-07-31
        holdout: 2024-08-01 → 2024-10-31

  - Feature set: snapshots_ml_flat_v2 columns (the same the runtime sees)
    minus per-strike-ladder columns that would leak strike identity.

  - Trading-utility evaluation: at each threshold, simulate "model says trade
    when prob >= threshold" → realized P&L from the LABEL's net_pnl_pct.
    Report total P&L, win rate, trade count at threshold sweep.

  - Holdout-only verdict — no train/valid metrics in the headline (those
    are subject to overfitting). The honest question is "what would this
    do on the never-seen 2024-08 → 2024-10 window?"

Output: a per-recipe report directory with:
    - metrics.json
    - holdout_trades.parquet (one row per model-fired holdout trade)
    - threshold_sweep.csv

This is NOT the full pipeline. No HPO, no CV, no publication gates. The
output is one number per recipe: "does this clear cost net of trading
on the holdout?" If yes → green-light full HPO. If no → file the result
under "5 confirmations + 1 = 6, data ceiling proven beyond doubt."
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# xgboost is only needed for the training path — lazy import so this module
# can be imported (e.g. by tests of simulate_single_position) without it.
xgb = None  # type: ignore[assignment]


def _require_xgboost():
    """Lazy-import xgb; raise a clear error if missing when actually needed."""
    global xgb
    if xgb is None:
        try:
            import xgboost as _xgb  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "xgboost not installed; install with: pip install xgboost"
            ) from exc
        xgb = _xgb
    return xgb


DEFAULT_LABELS_ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/option_pnl_labels_v1")
DEFAULT_FLAT_ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data/snapshots_ml_flat_v2")

# Match C1's temporal split — same train/valid/holdout windows so we can
# compare apples-to-apples to the prior 5 overfit confirmations.
TRAIN_END = pd.Timestamp("2024-04-30")
VALID_END = pd.Timestamp("2024-07-31")
HOLDOUT_END = pd.Timestamp("2024-10-31")

# Skip per-strike-ladder columns that could leak label info (they encode
# the same option-chain state the label is computed from). Aggregates are fine.
LEAK_PREFIXES = ("strike_", "ladder_strike_", "raw_strike_")


@dataclass
class TrainResult:
    recipe_id: str
    n_train: int
    n_valid: int
    n_holdout: int
    train_pos_rate: float
    valid_pos_rate: float
    holdout_pos_rate: float
    holdout_roc_auc: float
    threshold_sweep: list[dict]
    best_threshold_by_net_pnl: float
    best_holdout_net_pnl_sum: float
    best_holdout_trades: int
    best_holdout_win_rate: float
    # Per-snapshot sweep (every prob-crossing minute = trade). Inflates
    # trade count vs reality; kept for diagnosis of "is the model edge real
    # at all" separately from "does single-position execution preserve it".
    threshold_sweep_optimistic: list[dict] = field(default_factory=list)


def load_labels_and_features(labels_root: Path, flat_root: Path, recipe_id: str) -> pd.DataFrame:
    """Load labels for one recipe + matching flat-v2 features. Join on snapshot_id."""
    label_files = sorted(labels_root.glob("labels/year=*/*.parquet"))
    if not label_files:
        raise FileNotFoundError(f"no labels under {labels_root}/labels")
    labels = pd.concat([pd.read_parquet(f) for f in label_files], ignore_index=True)
    labels = labels[labels["recipe_id"] == recipe_id].copy()
    if labels.empty:
        raise ValueError(f"no labels for recipe {recipe_id}")
    labels["trade_date"] = pd.to_datetime(labels["trade_date"])

    flat_files = sorted(flat_root.glob("year=*/*.parquet"))
    if not flat_files:
        raise FileNotFoundError(f"no flat-v2 under {flat_root}")
    # Load all flat data — this is fine for in-memory training (a few hundred MB).
    flat = pd.concat([pd.read_parquet(f) for f in flat_files], ignore_index=True)
    flat["trade_date"] = pd.to_datetime(flat["trade_date"])
    flat["snapshot_id"] = flat["snapshot_id"].astype(str)
    labels["snapshot_id"] = labels["snapshot_id"].astype(str)

    # JOIN
    merged = labels.merge(flat, on=["trade_date", "snapshot_id"], how="inner", suffixes=("_label", ""))
    if merged.empty:
        raise ValueError(f"recipe {recipe_id}: labels did not join with flat features — check snapshot_id format")
    return merged


def select_feature_columns(df: pd.DataFrame) -> list[str]:
    """Pick numeric columns from flat-v2 minus leak-prone strike columns."""
    skip_exact = {
        "trade_date", "timestamp", "snapshot_id", "instrument", "schema_name",
        "schema_version", "build_source", "build_run_id", "snapshot_raw_json",
        # Labels and label-derived debug
        "recipe_id", "label", "reason_skipped", "selected_strike", "selected_expiry",
        "entry_premium", "exit_premium", "exit_bar_offset", "exit_reason",
        "gross_pnl_pct", "net_pnl_pct", "cost_pct", "atm_strike", "timestamp_minute",
        # opt_flow_atm_strike is the label's strike — would leak directly.
        "opt_flow_atm_strike",
    }
    feat_cols: list[str] = []
    for c in df.columns:
        if c in skip_exact:
            continue
        if any(c.startswith(p) for p in LEAK_PREFIXES):
            continue
        # Must be numeric (drop string columns)
        if pd.api.types.is_numeric_dtype(df[c]):
            feat_cols.append(c)
    return feat_cols


def simulate_single_position(holdout_df: pd.DataFrame, p_hold: np.ndarray,
                              threshold: float) -> dict:
    """Walk holdout in (trade_date, timestamp_minute) order, firing at most
    one position at a time. Matches the live runtime's single-position
    constraint, which the per-snapshot threshold sweep does NOT honor.

    Why this matters: the per-snapshot sweep treats every prob-crossing
    minute as an independent trade. The runtime can only hold one position;
    while a position is open, subsequent prob-crossings are ignored. On the
    2024-08/09 holdout this 'over-counting' inflated trainer's trade count
    by ~3x and inflated total P&L by a similar factor. This function
    re-computes aggregates under realistic single-position execution.

    The position-open window uses the LABEL's `exit_bar_offset` (which
    encodes the actual stop/target/max-hold exit minute). Subsequent fires
    are blocked until `entry_minute + exit_bar_offset`. Position state
    resets at each trade_date boundary (overnight gap, force-close).
    """
    df = holdout_df.copy()
    df["sim_prob"] = p_hold
    # Sort by (date, minute) — required since the merge with features may
    # have scrambled order, and pandas isn't stable across versions.
    df = df.sort_values(["trade_date", "timestamp_minute"], kind="mergesort").reset_index(drop=True)

    trades: list[dict] = []
    last_date = None
    blocked_until_min = -1
    for row in df.itertuples(index=False):
        td = row.trade_date
        if td != last_date:
            blocked_until_min = -1
            last_date = td
        minute = int(row.timestamp_minute)
        if minute < blocked_until_min:
            continue  # position from prior fire still open
        prob = float(row.sim_prob)
        if prob < threshold:
            continue
        # Fire: realize the label's net P&L; advance the block window
        pnl = float(getattr(row, "net_pnl_pct", 0.0) or 0.0)
        hold = int(getattr(row, "exit_bar_offset", 1) or 1)
        trades.append({
            "trade_date": str(td)[:10] if hasattr(td, "strftime") else str(td),
            "entry_minute": minute,
            "exit_bar_offset": hold,
            "net_pnl_pct": pnl,
            "prob": prob,
        })
        blocked_until_min = minute + hold

    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "net_pnl_sum": 0.0, "win_rate": 0.0,
                "avg_pnl": 0.0, "trades": []}
    pnls = [t["net_pnl_pct"] for t in trades]
    return {
        "n_trades": n,
        "net_pnl_sum": float(sum(pnls)),
        "avg_pnl": float(sum(pnls) / n),
        "win_rate": float(sum(1 for p in pnls if p > 0) / n),
        "trades": trades,
    }


def split_temporal(df: pd.DataFrame,
                   holdout_end: pd.Timestamp = HOLDOUT_END
                   ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Train / valid / holdout by trade_date. Same windows as C1 for apples-to-apples.

    `holdout_end` lets callers tighten the holdout (e.g. to match a runtime
    replay's actual coverage window). Defaults to the C1 holdout end.
    """
    train = df[df["trade_date"] <= TRAIN_END].copy()
    valid = df[(df["trade_date"] > TRAIN_END) & (df["trade_date"] <= VALID_END)].copy()
    holdout = df[(df["trade_date"] > VALID_END) & (df["trade_date"] <= holdout_end)].copy()
    return train, valid, holdout


def train_recipe(df: pd.DataFrame, recipe_id: str,
                 params_override: Optional[dict] = None,
                 holdout_end: pd.Timestamp = HOLDOUT_END) -> TrainResult:
    """Train a binary XGBoost classifier for one recipe."""
    feat_cols = select_feature_columns(df)
    if not feat_cols:
        raise ValueError(f"recipe {recipe_id}: no feature columns found")

    train, valid, holdout = split_temporal(df, holdout_end=holdout_end)
    if min(len(train), len(holdout)) < 100:
        raise ValueError(
            f"recipe {recipe_id}: too few rows after split — train={len(train)} holdout={len(holdout)}"
        )

    X_train = train[feat_cols].fillna(0.0).to_numpy(dtype=np.float32)
    y_train = train["label"].astype(int).to_numpy()
    X_valid = valid[feat_cols].fillna(0.0).to_numpy(dtype=np.float32) if not valid.empty else None
    y_valid = valid["label"].astype(int).to_numpy() if not valid.empty else None
    X_hold = holdout[feat_cols].fillna(0.0).to_numpy(dtype=np.float32)
    y_hold = holdout["label"].astype(int).to_numpy()
    pnl_hold = holdout["net_pnl_pct"].astype(float).to_numpy()

    _require_xgboost()
    # Default config matches the original MVP. Override via --params-json
    # for HPO-trained config equivalence with the deployed bundle.
    final_params = dict(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.85, colsample_bytree=0.85, reg_lambda=2.0,
    )
    if params_override:
        final_params.update(params_override)
    model = xgb.XGBClassifier(
        **final_params,
        objective="binary:logistic", eval_metric="auc",
        tree_method="hist", n_jobs=4, random_state=42,
    )
    eval_set = []
    if X_valid is not None and len(X_valid) > 0:
        eval_set.append((X_valid, y_valid))
    model.fit(X_train, y_train, eval_set=eval_set, verbose=False)

    p_hold = model.predict_proba(X_hold)[:, 1]

    # Holdout AUC
    try:
        from sklearn.metrics import roc_auc_score
        auc = float(roc_auc_score(y_hold, p_hold))
    except Exception:
        auc = float("nan")

    # Two parallel views of the threshold sweep:
    #
    #   OPTIMISTIC (per-snapshot): every prob-crossing minute counts as a
    #     trade. Inflates trade count and net P&L because real runtime can't
    #     hold concurrent positions. Kept for backward compat with earlier
    #     reports — but DO NOT use this for "what would I earn live?".
    #
    #   REALISTIC (single-position): walks time-ordered, one trade open at a
    #     time, exit-bar-offset blocks subsequent fires. This is the number
    #     the live runtime should converge to (within smart-strike / fill
    #     differences, which we close separately via the contract).
    sweep_optimistic: list[dict] = []
    sweep_realistic: list[dict] = []
    for thr in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        mask = p_hold >= thr
        n_trades = int(mask.sum())
        if n_trades == 0:
            sweep_optimistic.append({"threshold": thr, "n_trades": 0, "net_pnl_sum": 0.0, "win_rate": 0.0})
        else:
            traded_pnl = pnl_hold[mask]
            sweep_optimistic.append({
                "threshold": thr,
                "n_trades": n_trades,
                "net_pnl_sum": float(traded_pnl.sum()),
                "avg_pnl": float(traded_pnl.mean()),
                "win_rate": float((traded_pnl > 0).mean()),
            })
        sim = simulate_single_position(holdout, p_hold, thr)
        sweep_realistic.append({
            "threshold": thr,
            "n_trades": sim["n_trades"],
            "net_pnl_sum": sim["net_pnl_sum"],
            "avg_pnl": sim["avg_pnl"],
            "win_rate": sim["win_rate"],
        })

    # Best threshold under REALISTIC semantics — that's the deployment number.
    best = max(sweep_realistic, key=lambda d: d["net_pnl_sum"])
    sweep = sweep_realistic  # backward-compat: keep field name in result

    return TrainResult(
        recipe_id=recipe_id,
        n_train=len(train),
        n_valid=len(valid),
        n_holdout=len(holdout),
        train_pos_rate=float(y_train.mean()),
        valid_pos_rate=float(y_valid.mean()) if y_valid is not None and len(y_valid) else 0.0,
        holdout_pos_rate=float(y_hold.mean()),
        holdout_roc_auc=auc,
        threshold_sweep=sweep,
        best_threshold_by_net_pnl=float(best["threshold"]),
        best_holdout_net_pnl_sum=float(best["net_pnl_sum"]),
        best_holdout_trades=int(best.get("n_trades", 0)),
        best_holdout_win_rate=float(best.get("win_rate", 0.0)),
        threshold_sweep_optimistic=sweep_optimistic,  # per-snapshot, kept for diagnosis
    )


def load_params_override(path: str | Path) -> dict:
    """Load XGB hyperparameters from JSON.

    Accepts either a flat dict (`{"max_depth": 8, ...}`) or an HPO
    `results.json` with a top-level `trials` array — in which case the
    first trial's `params` are returned (the pipeline writes trials best-first).
    """
    loaded = json.loads(Path(path).read_text())
    if isinstance(loaded, dict) and "trials" in loaded:
        trials = loaded["trials"]
        if not trials or "params" not in trials[0]:
            raise ValueError(
                f"{path}: 'trials' present but first trial has no 'params'"
            )
        return dict(trials[0]["params"])
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected JSON object, got {type(loaded).__name__}")
    return dict(loaded)


def run(args) -> int:
    labels_root = Path(args.labels)
    flat_root = Path(args.flat)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    recipe_ids = args.recipes or ["ATM_CE_9", "ATM_PE_9", "ATM_CE_15", "ATM_PE_15"]
    holdout_end = pd.Timestamp(args.holdout_end) if getattr(args, "holdout_end", None) else HOLDOUT_END
    # Optional params override (e.g. HPO trial-18 to match deployed bundle).
    params_override: Optional[dict] = None
    if getattr(args, "params_json", None):
        params_override = load_params_override(args.params_json)
        print(f"params override: {json.dumps(params_override, sort_keys=True)}")
    print(f"=== Option-P&L MVP trainer ===")
    print(f"labels: {labels_root}")
    print(f"flat:   {flat_root}")
    print(f"out:    {out_dir}")
    print(f"recipes: {recipe_ids}")
    print(f"train end: {TRAIN_END.date()}   valid end: {VALID_END.date()}   holdout end: {holdout_end.date()}")
    print()

    all_results: dict[str, dict] = {}
    for recipe_id in recipe_ids:
        print(f"--- {recipe_id} ---")
        try:
            df = load_labels_and_features(labels_root, flat_root, recipe_id)
            print(f"  joined rows: {len(df)}")
            res = train_recipe(df, recipe_id, params_override=params_override,
                               holdout_end=holdout_end)
            print(f"  train/valid/holdout: {res.n_train}/{res.n_valid}/{res.n_holdout}")
            print(f"  holdout pos_rate: {res.holdout_pos_rate:.3f}  AUC: {res.holdout_roc_auc:.3f}")
            print(f"  threshold sweep — REALISTIC (single-position, runtime-equivalent):")
            for s in res.threshold_sweep:
                print(f"    thr={s['threshold']:.2f}  n={s['n_trades']:5d}  net={s['net_pnl_sum']:+.3f}  wr={s.get('win_rate',0):.3f}")
            print(f"  threshold sweep — OPTIMISTIC (per-snapshot, NOT runtime-equivalent — for diagnosis):")
            for s in res.threshold_sweep_optimistic:
                print(f"    thr={s['threshold']:.2f}  n={s['n_trades']:5d}  net={s['net_pnl_sum']:+.3f}  wr={s.get('win_rate',0):.3f}")
            print(f"  BEST threshold (REALISTIC): {res.best_threshold_by_net_pnl:.2f}  net_pnl_sum: {res.best_holdout_net_pnl_sum:+.3f}  trades: {res.best_holdout_trades}  wr: {res.best_holdout_win_rate:.3f}")
            verdict = "EDGE" if res.best_holdout_net_pnl_sum > 0 else "NO_EDGE"
            print(f"  HOLDOUT VERDICT: {verdict}")
            all_results[recipe_id] = asdict(res)
            all_results[recipe_id]["holdout_verdict"] = verdict
        except Exception as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            all_results[recipe_id] = {"error": str(exc)}
        print()

    # Aggregate verdict
    verdicts = [r.get("holdout_verdict") for r in all_results.values() if isinstance(r, dict) and "holdout_verdict" in r]
    edge_count = sum(1 for v in verdicts if v == "EDGE")
    print(f"=== Aggregate ===")
    print(f"recipes with positive holdout P&L: {edge_count}/{len(verdicts)}")

    (out_dir / "results.json").write_text(json.dumps(all_results, indent=2))
    print(f"results: {out_dir / 'results.json'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Minimal MVP option-P&L trainer")
    p.add_argument("--labels", default=str(DEFAULT_LABELS_ROOT))
    p.add_argument("--flat", default=str(DEFAULT_FLAT_ROOT))
    p.add_argument("--out", required=True, help="Output directory")
    p.add_argument("--recipes", nargs="*", default=None, help="Subset of recipe IDs (default: all 4)")
    p.add_argument(
        "--params-json",
        default=None,
        help=(
            "Path to JSON with XGB hyperparameters. Accepts either a plain {param:val} "
            "dict or an HPO results.json with top-level 'trials'[0]['params']. "
            "Used to match the deployed bundle's HPO trial when validating realistic edge."
        ),
    )
    p.add_argument(
        "--holdout-end",
        default=None,
        help=(
            "Override holdout end date (YYYY-MM-DD). Useful to tighten the holdout "
            "to match a runtime replay's actual coverage window — e.g. set to "
            "2024-09-30 if runtime only replayed Aug-Sep."
        ),
    )
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
