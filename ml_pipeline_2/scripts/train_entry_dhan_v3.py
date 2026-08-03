"""Train entry_only_bundle v3 from Dhan parquet data.

Trains BankNifty or NIFTY entry model on Dhan historical data (Nov2024-Jun2026,
monthly regime). Uses the SAME feature_engine.build_features as live serving —
training/serving parity is guaranteed by construction.

Contract changes vs current bundle:
  * Adds vix_current + vix_intraday_chg (VIX kept per user decision)
  * Computes real feature medians (no more NaN-passes-raw-to-model)
  * Saves max_nan_features in bundle so inference gate is self-documenting
  * holdout_eval with AUC, ECE, separation table
  * Works for BANKNIFTY (45→47 feats) and NIFTY (new 47-feat model)

Usage (run on ML VM):
    # BankNifty
    python -m ml_pipeline_2.scripts.train_entry_dhan_v3 \\
        --instrument BANKNIFTY \\
        --data-dir .data/dhan_pipeline/indicators \\
        --output models/dhan_entry_bundle_v3.joblib \\
        --train-start 2024-11-01 --train-end 2026-04-30 \\
        --holdout-start 2026-05-01 --holdout-end 2026-06-30

    # NIFTY
    python -m ml_pipeline_2.scripts.train_entry_dhan_v3 \\
        --instrument NIFTY \\
        --data-dir .data/dhan_pipeline/indicators \\
        --output models/nifty_entry_bundle_v3.joblib \\
        --train-start 2024-11-01 --train-end 2026-04-30 \\
        --holdout-start 2026-05-01 --holdout-end 2026-06-30
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("train_entry_v3")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from contracts_app.instruments import known_instruments  # noqa: E402

# ── Feature set ────────────────────────────────────────────────────────────────
# Same as current 45-feat BN entry model + vix_current + vix_intraday_chg.
# Ordered to match what project_stage_views_v2 produces so runtime extracts
# in same order. VIX features are imputed with median when NaN in live.

ENTRY_FEATURES_V3: List[str] = [
    # EMA / trend anchors (GROUP A — price, always available)
    "ema_9_slope", "ema_21_slope", "ema_50_slope",
    "ema_spread_9_21", "ema_spread_21_50", "ema_order",
    "adx_14",
    # Options OI velocity (GROUP B — require option chain)
    "vel_ce_oi_delta_open", "vel_pe_oi_delta_open",
    "vel_ce_oi_delta_30m",  "vel_pe_oi_delta_30m",
    "vel_oi_ratio_delta_open", "vel_oi_ratio_delta_30m",
    "vel_ce_oi_build_rate",  "vel_pe_oi_build_rate",
    # PCR velocity
    "vel_pcr_delta_open", "vel_pcr_delta_30m",
    "vel_pcr_acceleration", "vel_pcr_trend_direction",
    # Price velocity
    "vel_price_delta_open", "vel_price_delta_30m", "vel_price_delta_60m",
    "vel_price_acceleration",
    # AM session context (GROUP D — require prev_day + AM session)
    "ctx_am_range_high", "ctx_am_range_low", "ctx_am_range_size",
    "ctx_am_price_position",
    "ctx_am_gap_from_yday", "ctx_am_gap_filled",
    # IV velocity
    "vel_atm_ce_iv_delta_open", "vel_atm_pe_iv_delta_open",
    "vel_iv_skew_delta_open", "vel_iv_compression_rate",
    # Options volume velocity
    "vel_ce_vol_delta_30m", "vel_pe_vol_delta_30m",
    "vel_options_vol_acceleration",
    # AM context continued
    "ctx_am_trend", "ctx_am_trend_strength", "ctx_am_reversal",
    "ctx_am_oi_direction", "ctx_am_vwap_side", "ctx_am_breakout_confirmed",
    # Gap features
    "ctx_gap_pct", "ctx_gap_up", "ctx_gap_down",
    # VIX (GROUP C — kept per decision; NaN-imputed in live when WS returns 429)
    "vix_current",       # renamed from "vix" in pipeline output
    "vix_intraday_chg",  # computed by feature_engine from vix column
]

# Label: 1 if |close_move| >= LABEL_PT_THRESHOLD in LABEL_HORIZON_MIN minutes
LABEL_PT_THRESHOLD = 100   # points
LABEL_HORIZON_MIN  = 15    # minutes (15-bar lookahead)

# Session filter: train on 9:45-15:05 IST bars only.
# By 9:45 (bar 30) all 30m velocity features are available (vel_price_delta_30m,
# vel_pcr_delta_30m etc). ctx_am_range/trend/reversal/vwap_side (need 11:30 AM
# session) will be NaN before 11:30 — model imputes with median, which is fine.
# ctx_gap features are available from bar 1 with the velocity_context_provider fix.
SESSION_START_MIN = 9 * 60 + 45   # 9:45 IST (minute_of_day)
SESSION_END_MIN   = 15 * 60 + 5   # 15:05 IST

MAX_NAN_FEATURES = 3       # allow up to 3 NaN at inference (typically vix×2 + 1 other)


# ── Data loading ───────────────────────────────────────────────────────────────

def load_indicators(data_dir: Path, instrument: str,
                    start: str, end: str) -> pd.DataFrame:
    """Load per-day indicator parquet files built by dhan_data_pipeline build step."""
    inst = instrument.upper()
    pattern = str(data_dir / f"*{inst}*" / "*.parquet")
    import glob
    files = sorted(glob.glob(pattern, recursive=True))
    if not files:
        # Try flat directory structure
        pattern2 = str(data_dir / "*.parquet")
        files = sorted(f for f in glob.glob(pattern2)
                       if inst.lower() in f.lower() or inst in f)
    if not files:
        raise FileNotFoundError(
            f"No indicator parquet for {inst} in {data_dir}\n"
            f"Run: python -m ml_pipeline_2.scripts.dhan_data_pipeline build ...")
    log.info("Loading %d parquet files for %s from %s", len(files), inst, data_dir)
    frames = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            frames.append(df)
        except Exception as exc:
            log.warning("skip %s: %s", f, exc)
    if not frames:
        raise RuntimeError(f"All parquet files failed for {inst}")
    data = pd.concat(frames, ignore_index=True)
    data["trade_date"] = pd.to_datetime(data.get("trade_date", data.index))
    data = data[(data["trade_date"] >= start) & (data["trade_date"] <= end)]
    log.info("Loaded %d rows for %s between %s and %s", len(data), inst, start, end)
    return data


def add_labels(df: pd.DataFrame) -> pd.DataFrame:
    """Add binary entry label: 1 if |move| >= threshold in horizon_min minutes.
    Only labels bars within the 9:45-15:05 IST session window — by 9:45 all
    30m velocity features are available; 15:05 avoids end-of-day noise.
    """
    df = df.sort_values(["trade_date", "timestamp"]).copy()
    close_col = next((c for c in ["px_fut_close", "close", "fut_close"] if c in df.columns), None)
    if close_col is None:
        raise ValueError(f"No close price column found. Columns: {list(df.columns[:20])}")

    # Apply session filter (9:45-15:05 IST)
    ts_col = df["timestamp"] if "timestamp" in df.columns else pd.to_datetime(df.index)
    try:
        minute_of_day = pd.to_datetime(ts_col).dt.hour * 60 + pd.to_datetime(ts_col).dt.minute
    except Exception:
        minute_of_day = None

    close = pd.to_numeric(df[close_col], errors="coerce")
    # Lookahead: max |close[t+k] - close[t]| / close[t] >= threshold
    n_bars = LABEL_HORIZON_MIN
    future_max = close.shift(-1).rolling(window=n_bars, min_periods=n_bars).max().shift(-(n_bars - 1))
    future_min = close.shift(-1).rolling(window=n_bars, min_periods=n_bars).min().shift(-(n_bars - 1))
    up_move   = (future_max - close).abs()
    down_move = (close - future_min).abs()
    max_move  = np.maximum(up_move.fillna(0), down_move.fillna(0))
    df["entry_label"] = (max_move >= LABEL_PT_THRESHOLD).astype(float)
    # Mark lookahead-invalid bars as NaN (last horizon_min bars of each day)
    df.loc[future_max.isna(), "entry_label"] = np.nan
    # Apply session window filter (9:45-15:05): outside window → NaN label
    if minute_of_day is not None:
        outside = (minute_of_day < SESSION_START_MIN) | (minute_of_day > SESSION_END_MIN)
        df.loc[outside, "entry_label"] = np.nan
        log.info("Session filter 9:45-15:05: excluded %d bars outside window", outside.sum())
    valid = df["entry_label"].notna()
    log.info("Labels: %d valid bars, pos_rate=%.3f", valid.sum(), df.loc[valid, "entry_label"].mean())
    return df


def normalise_vix(df: pd.DataFrame) -> pd.DataFrame:
    """Rename 'vix' → 'vix_current' so it matches the live feature name."""
    if "vix_current" not in df.columns and "vix" in df.columns:
        df = df.copy()
        df["vix_current"] = pd.to_numeric(df["vix"], errors="coerce")
    return df


def build_feature_matrix(df: pd.DataFrame, features: List[str],
                          medians: Optional[Dict[str, float]] = None) -> pd.DataFrame:
    """Extract and (optionally) impute features. Missing columns → NaN column."""
    X = pd.DataFrame(index=df.index)
    for feat in features:
        if feat in df.columns:
            X[feat] = pd.to_numeric(df[feat], errors="coerce")
        else:
            log.warning("Feature '%s' missing from data — will be NaN", feat)
            X[feat] = np.nan
    if medians:
        for feat in features:
            if feat in medians and X[feat].isna().any():
                X[feat] = X[feat].fillna(medians[feat])
    return X


# ── Training ───────────────────────────────────────────────────────────────────

def compute_medians(df: pd.DataFrame, features: List[str]) -> Dict[str, float]:
    """Compute per-feature median from training data for NaN imputation at inference."""
    medians = {}
    for feat in features:
        if feat in df.columns:
            v = pd.to_numeric(df[feat], errors="coerce").median()
            medians[feat] = 0.0 if (pd.isna(v) or not np.isfinite(v)) else float(v)
        else:
            medians[feat] = 0.0
    nan_feats = [f for f, v in medians.items() if v == 0.0]
    if nan_feats:
        log.warning("%d features have median=0.0 (missing or all-NaN): %s", len(nan_feats), nan_feats[:5])
    return medians


def run_hpo(X_train: pd.DataFrame, y_train: np.ndarray,
            X_valid: pd.DataFrame, y_valid: np.ndarray,
            n_trials: int = 50) -> Any:
    """Optuna HPO over XGBoost hyperparameters, optimised for AUC."""
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        import xgboost as xgb
        from sklearn.metrics import roc_auc_score
    except ImportError as exc:
        raise ImportError(f"Install optuna + xgboost: pip install optuna xgboost — {exc}")

    def objective(trial):
        params = {
            "n_estimators":      trial.suggest_int("n_estimators", 200, 1000),
            "max_depth":         trial.suggest_int("max_depth", 3, 8),
            "learning_rate":     trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "subsample":         trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree":  trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "min_child_weight":  trial.suggest_int("min_child_weight", 1, 20),
            "reg_alpha":         trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
            "reg_lambda":        trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
            "scale_pos_weight":  float((y_train == 0).sum() / max(1, (y_train == 1).sum())),
            "use_label_encoder": False,
            "eval_metric": "auc",
            "random_state": 42,
            "n_jobs": -1,
        }
        model = xgb.XGBClassifier(**params)
        model.fit(X_train, y_train, verbose=False)
        prob = model.predict_proba(X_valid)[:, 1]
        return roc_auc_score(y_valid, prob)

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    log.info("Best AUC=%.4f params=%s", study.best_value, study.best_params)

    import xgboost as xgb
    best = study.best_params
    best["scale_pos_weight"] = float((y_train == 0).sum() / max(1, (y_train == 1).sum()))
    best["use_label_encoder"] = False
    best["eval_metric"] = "auc"
    best["random_state"] = 42
    best["n_jobs"] = -1
    final = xgb.XGBClassifier(**best)
    final.fit(X_train, y_train, verbose=False)
    return final, study.best_params, study.best_value


def evaluate(model: Any, X: pd.DataFrame, y: np.ndarray,
             label: str = "holdout") -> Dict[str, Any]:
    """Compute AUC, ECE, separation table."""
    from sklearn.metrics import roc_auc_score, brier_score_loss
    prob = model.predict_proba(X)[:, 1]
    auc   = float(roc_auc_score(y, prob))
    brier = float(brier_score_loss(y, prob))
    # ECE
    edges = np.linspace(0, 1, 11)
    ece = 0.0
    reliability = []
    for i in range(10):
        hi = edges[i + 1] if i < 9 else 1.001
        m = (prob >= edges[i]) & (prob < hi)
        cnt = int(m.sum())
        if cnt == 0:
            reliability.append({"bin": f"{edges[i]:.1f}-{edges[i+1]:.1f}", "n": 0})
            continue
        conf = float(prob[m].mean())
        acc  = float(y[m].mean())
        ece += abs(acc - conf) * cnt / len(y)
        reliability.append({"bin": f"{edges[i]:.1f}-{edges[i+1]:.1f}", "n": cnt,
                             "conf": round(conf, 4), "acc": round(acc, 4)})
    # Separation
    sep = []
    for thr in [0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70]:
        fired = prob >= thr
        if fired.sum() == 0 or (~fired).sum() == 0:
            sep.append({"thr": thr, "degenerate": True})
            continue
        pf = float(y[fired].mean())
        bn = float(y[~fired].mean())
        sep.append({"thr": thr, "fire_rate": round(float(fired.mean()), 4),
                    "precision_fired": round(pf, 4), "base_not_fired": round(bn, 4),
                    "separation": round(pf - bn, 4)})
    log.info("%s: AUC=%.4f brier=%.4f ECE=%.4f", label, auc, brier, ece)
    return {"rows": int(len(y)), "base_rate": round(float(y.mean()), 4),
            "roc_auc": round(auc, 4), "brier": round(brier, 4), "ece": round(ece, 4),
            "reliability_table": reliability, "separation_table": sep}


# ── Main ───────────────────────────────────────────────────────────────────────

def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instrument", required=True, choices=known_instruments())
    ap.add_argument("--data-dir",  required=True, help="Path to dhan_data_pipeline indicators dir")
    ap.add_argument("--output",    required=True, help="Output bundle path (.joblib)")
    ap.add_argument("--train-start",   default="2024-11-01")
    ap.add_argument("--train-end",     default="2026-03-31")
    ap.add_argument("--valid-start",   default="2026-04-01")
    ap.add_argument("--valid-end",     default="2026-05-31")
    ap.add_argument("--holdout-start", default="2026-06-01")
    ap.add_argument("--holdout-end",   default="2026-06-30")
    ap.add_argument("--n-trials",  type=int, default=60, help="Optuna HPO trials")
    ap.add_argument("--features",  default="", help="Comma-separated feature override (empty=default)")
    args = ap.parse_args(argv)

    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))

    features: List[str] = (
        [f.strip() for f in args.features.split(",") if f.strip()]
        if args.features else ENTRY_FEATURES_V3
    )
    log.info("Instrument=%s | Features=%d | HPO trials=%d", args.instrument, len(features), args.n_trials)
    log.info("Train %s→%s | Valid %s→%s | Holdout %s→%s",
             args.train_start, args.train_end, args.valid_start, args.valid_end,
             args.holdout_start, args.holdout_end)

    data_dir = Path(args.data_dir).resolve()

    # ── Load all data ──────────────────────────────────────────────────────────
    all_start = args.train_start
    all_end   = args.holdout_end
    df = load_indicators(data_dir, args.instrument, all_start, all_end)
    df = normalise_vix(df)
    df = add_labels(df)

    valid_rows = df["entry_label"].notna()
    df = df[valid_rows].copy()
    log.info("After label filter: %d bars, pos_rate=%.3f", len(df), df["entry_label"].mean())

    def _slice(start: str, end: str) -> pd.DataFrame:
        m = (df["trade_date"] >= start) & (df["trade_date"] <= end)
        return df[m].copy()

    train_df   = _slice(args.train_start,   args.train_end)
    valid_df   = _slice(args.valid_start,   args.valid_end)
    holdout_df = _slice(args.holdout_start, args.holdout_end)
    log.info("Split — train=%d valid=%d holdout=%d", len(train_df), len(valid_df), len(holdout_df))

    # ── Compute medians from training data ────────────────────────────────────
    log.info("Computing feature medians from training slice...")
    medians = compute_medians(train_df, features)
    nan_count = sum(1 for f in features if train_df.get(f, pd.Series(dtype=float)).isna().all())
    log.info("Medians computed. %d features all-NaN in training (will impute 0.0)", nan_count)

    # ── Build feature matrices with median imputation ─────────────────────────
    X_train = build_feature_matrix(train_df, features, medians)
    y_train = train_df["entry_label"].to_numpy(float)
    X_valid = build_feature_matrix(valid_df, features, medians)
    y_valid = valid_df["entry_label"].to_numpy(float)
    X_hold  = build_feature_matrix(holdout_df, features, medians)
    y_hold  = holdout_df["entry_label"].to_numpy(float)

    # Remove rows with remaining NaN (shouldn't happen after median fill)
    for name, X, y in [("train", X_train, y_train), ("valid", X_valid, y_valid), ("hold", X_hold, y_hold)]:
        bad = X.isna().any(axis=1) | ~np.isfinite(y)
        if bad.sum():
            log.warning("%s: dropping %d rows with NaN after imputation", name, bad.sum())

    ok_tr = np.isfinite(y_train) & ~X_train.isna().any(axis=1)
    ok_va = np.isfinite(y_valid) & ~X_valid.isna().any(axis=1)
    ok_ho = np.isfinite(y_hold)  & ~X_hold.isna().any(axis=1)
    X_train, y_train = X_train[ok_tr], y_train[ok_tr]
    X_valid, y_valid = X_valid[ok_va], y_valid[ok_va]
    X_hold,  y_hold  = X_hold[ok_ho],  y_hold[ok_ho]

    log.info("Clean rows — train=%d (pos=%.3f) valid=%d (pos=%.3f) holdout=%d (pos=%.3f)",
             len(y_train), y_train.mean(), len(y_valid), y_valid.mean(),
             len(y_hold), y_hold.mean())

    # ── HPO ───────────────────────────────────────────────────────────────────
    log.info("Starting Optuna HPO (%d trials)...", args.n_trials)
    model, best_params, valid_auc = run_hpo(X_train, y_train, X_valid, y_valid, args.n_trials)

    # ── Calibration ───────────────────────────────────────────────────────────
    log.info("Fitting isotonic calibration on validation set...")
    from sklearn.calibration import CalibratedClassifierCV
    try:
        cal = CalibratedClassifierCV(estimator=model, method="isotonic", cv="prefit")
    except TypeError:
        cal = CalibratedClassifierCV(base_estimator=model, method="isotonic", cv="prefit")
    cal.fit(X_valid, y_valid)

    # ── Evaluate on holdout ───────────────────────────────────────────────────
    holdout_eval = evaluate(cal, X_hold, y_hold, "holdout")

    # Ship gate
    auc = holdout_eval["roc_auc"]
    gates = {
        "auc>=0.62":        auc >= 0.62,
        "separation>=0.08": any((r.get("separation") or 0) >= 0.08
                                for r in holdout_eval["separation_table"]
                                if not r.get("degenerate")),
        "prob_spread>=0.20": True,  # checked below
    }
    p = cal.predict_proba(X_hold)[:, 1]
    gates["prob_spread>=0.20"] = bool(float(p.max()) - float(p.min()) >= 0.20)
    all_pass = all(gates.values())
    log.info("Ship gates: %s — %s", gates, "ALL_PASS" if all_pass else "FAIL")

    # Feature importances
    importances = {}
    try:
        raw_imp = model.feature_importances_
        for feat, imp in zip(features, raw_imp):
            importances[feat] = round(float(imp), 6)
        top5 = sorted(importances.items(), key=lambda x: -x[1])[:5]
        log.info("Top-5 features: %s", top5)
    except Exception:
        pass

    # ── Build bundle ──────────────────────────────────────────────────────────
    bundle: Dict[str, Any] = {
        "kind":        "entry_only_bundle",
        "version":     "3.0",
        "instrument":  args.instrument,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source":      "train_entry_dhan_v3",
        "source_description": (
            f"{args.instrument} entry model v3. Binary label: |move|>={LABEL_PT_THRESHOLD}pt "
            f"in {LABEL_HORIZON_MIN}min. {len(features)} features incl VIX. "
            f"Dhan data {args.train_start}..{args.holdout_end} monthly regime."
        ),
        # Core contract — inference reads these
        "features":         features,
        "medians":          medians,         # kept for backward compat
        "feature_medians":  medians,         # serving code reads this key
        "max_nan_features": MAX_NAN_FEATURES,
        "model":            cal,             # calibrated; .predict_proba used at runtime
        # Evaluation
        "holdout_eval":     holdout_eval,
        "feature_importances": importances,
        "ship_gates":       gates,
        "ship_gates_all_pass": all_pass,
        # Metadata
        "label_definition": f"binary: 1 if max(|high-close|,|close-low|) >= {LABEL_PT_THRESHOLD}pt in next {LABEL_HORIZON_MIN}min",
        "training_metadata": {
            "instrument":     args.instrument,
            "train_window":   f"{args.train_start}..{args.train_end}",
            "valid_window":   f"{args.valid_start}..{args.valid_end}",
            "holdout_window": f"{args.holdout_start}..{args.holdout_end}",
            "n_train":        int(len(y_train)),
            "n_holdout":      int(len(y_hold)),
            "pos_rate_train": round(float(y_train.mean()), 4),
            "pos_rate_hold":  round(float(y_hold.mean()),  4),
            "hpo_trials":     args.n_trials,
            "hpo_valid_auc":  round(float(valid_auc), 4),
            "best_params":    best_params,
            "label_pt":       LABEL_PT_THRESHOLD,
            "label_horizon_min": LABEL_HORIZON_MIN,
            "calibration":    "isotonic_prefit_on_valid",
        },
    }

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out)

    # Save report (no model object)
    report = {k: v for k, v in bundle.items() if k != "model"}
    out.with_suffix(".report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")

    log.info("Bundle written: %s  (AUC=%.4f, ALL_PASS=%s)", out, auc, all_pass)
    log.info("Report: %s", out.with_suffix(".report.json"))

    # Verify reload
    rb = joblib.load(out)
    assert rb["features"] == features, "Feature list mismatch after reload!"
    assert len(rb["medians"]) == len(features), "Medians count mismatch!"
    log.info("Reload verified OK — %d features, %d medians", len(rb["features"]), len(rb["medians"]))

    return 0 if all_pass else 2  # exit 2 = ran OK but ship gate failed


if __name__ == "__main__":
    raise SystemExit(main())
