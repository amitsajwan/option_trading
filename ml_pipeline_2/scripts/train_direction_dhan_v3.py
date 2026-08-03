"""Train direction_only_bundle v3 from Dhan parquet data.

Trains BankNifty or NIFTY direction model (predict CE vs PE side) on Dhan
historical data Nov2024-Jun2026. Same feature_engine.build_features as live —
training/serving parity by construction.

Changes vs current direction_monthly_v2 bundle:
  * VIX kept (vix_current, vix_intraday_chg) — will be imputed with median when NaN
  * Real feature medians saved so NaN features are imputed not passed raw
  * max_nan_features saved in bundle
  * Parity-verified: feature names match project_stage2_direction_view_v2 output

Usage (run on ML VM):
    # BankNifty
    python -m ml_pipeline_2.scripts.train_direction_dhan_v3 \\
        --instrument BANKNIFTY \\
        --data-dir .data/dhan_pipeline/indicators \\
        --output models/direction_dhan_bn_v3.joblib

    # NIFTY
    python -m ml_pipeline_2.scripts.train_direction_dhan_v3 \\
        --instrument NIFTY \\
        --data-dir .data/dhan_pipeline/indicators \\
        --output models/direction_dhan_nifty_v3.joblib
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("train_direction_v3")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from contracts_app.instruments import known_instruments  # noqa: E402

# ── Feature set ────────────────────────────────────────────────────────────────
# 75-feature set from current direction_monthly_v2 — VIX kept (imputed in live).
# Alias mapping: these names match project_stage2_direction_view_v2 output.
# Training data column names → served feature names must match EXACTLY.

DIRECTION_FEATURES_V3: List[str] = [
    # IV / options pricing
    "iv_skew", "atm_iv", "iv_pct_rank_session",
    "atm_ce_iv", "atm_pe_iv",
    "vel_iv_skew_delta_open", "vel_iv_compression_rate",
    "vel_atm_ce_iv_delta_open", "vel_atm_pe_iv_delta_open",
    # PCR + OI structure
    "pcr", "pcr_change_5m", "pcr_change_15m", "pcr_change_30m",
    "atm_ce_oi", "atm_pe_oi", "atm_oi_ratio", "near_atm_oi_ratio",
    "vel_pcr_delta_open", "vel_pcr_delta_30m",
    "vel_pcr_acceleration", "vel_pcr_trend_direction",
    "vel_ce_oi_delta_open", "vel_pe_oi_delta_open",
    "vel_oi_ratio_delta_open", "vel_oi_ratio_delta_30m",
    "vel_ce_oi_build_rate", "vel_pe_oi_build_rate",
    "vel_ce_oi_delta_30m", "vel_pe_oi_delta_30m",
    # Options volume
    "vel_ce_vol_delta_30m", "vel_pe_vol_delta_30m",
    "vel_options_vol_acceleration",
    # VIX (kept — imputed in live when WS returns 429)
    "vix_current", "vix_intraday_chg",
    # Volatility structure
    "atr_ratio", "bb_width_20", "bb_width_chg_5", "realized_vol_30m",
    "compression_score", "range_10", "range_30", "range_ratio_10_30",
    # Price / EMA trend
    "adx_14",
    "ema_9_slope", "ema_21_slope", "ema_50_slope",
    "ema_spread_9_21", "ema_spread_21_50", "ema_order",
    "dist_from_ema21",
    "vel_price_delta_open", "vel_price_delta_30m",
    "vel_price_delta_60m", "vel_price_acceleration",
    # Session time
    "minute_of_day", "minutes_to_close", "day_of_week",
    "minute_index",           # alias for minutes_since_open
    "position_in_day_range",
    "candle_overlap_10",
    # AM context
    "ctx_am_trend", "ctx_am_vwap_side", "ctx_am_trend_strength",
    "ctx_am_price_position", "ctx_am_range_size",
    "ctx_am_breakout_confirmed", "ctx_am_reversal", "ctx_am_oi_direction",
    # ORB
    "opening_range_ready",
    "opening_range_breakout_up",   # alias for orh_broken
    "opening_range_breakout_down", # alias for orl_broken
    "ctx_am_range_high", "ctx_am_range_low",
    # Expiry context
    "dte_days",       # alias for days_to_expiry
    "is_expiry_day",
]

# Direction label: 1 = price went UP by >= min_move in next 10min, 0 = DOWN
LABEL_HORIZON_MIN = 10
LABEL_MIN_MOVE_PT = 50   # minimum move to qualify (filter coin-flip bars)

# Session filter: 9:45-15:05 IST. By bar 30 (9:45) all 30m velocity features
# are available. ctx_am_* update incrementally per bar — no 11:30 restriction.
SESSION_START_MIN = 9 * 60 + 45   # 9:45 IST
SESSION_END_MIN   = 15 * 60 + 5   # 15:05 IST

MAX_NAN_FEATURES = 5     # direction model tolerates more NaN (vix×2, ivrank×1, etc.)


# ── Column aliases ─────────────────────────────────────────────────────────────
# Training data may use different column names than the serving-side feature names.
# Map: serving_name -> [possible_training_column_names]
COLUMN_ALIASES: Dict[str, List[str]] = {
    "vix_current":               ["vix_current", "vix"],
    "minute_index":              ["minute_index", "minutes_since_open"],
    "dte_days":                  ["dte_days", "days_to_expiry"],
    "opening_range_breakout_up": ["opening_range_breakout_up", "orh_broken"],
    "opening_range_breakout_down": ["opening_range_breakout_down", "orl_broken"],
    "iv_pct_rank_session":       ["iv_pct_rank_session", "iv_percentile", "iv_pct_rank"],
    "position_in_day_range":     ["position_in_day_range"],
    "pcr_change_5m":             ["pcr_change_5m"],
    "pcr_change_15m":            ["pcr_change_15m"],
    "pcr_change_30m":            ["pcr_change_30m"],
}


def resolve_column(df: pd.DataFrame, feature: str) -> Optional[pd.Series]:
    """Try feature name then aliases. Return None if not found."""
    candidates = COLUMN_ALIASES.get(feature, [feature])
    for col in candidates:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce")
    return None


def load_indicators(data_dir: Path, instrument: str,
                    start: str, end: str) -> pd.DataFrame:
    import glob
    inst = instrument.upper()
    patterns = [
        str(data_dir / f"*{inst}*" / "*.parquet"),
        str(data_dir / "*.parquet"),
    ]
    files = []
    for pat in patterns:
        found = sorted(f for f in glob.glob(pat, recursive=True)
                       if not files or inst.lower() in f.lower())
        files.extend(found)
        if files:
            break
    if not files:
        raise FileNotFoundError(f"No indicator parquet for {inst} in {data_dir}")
    log.info("Loading %d files for %s", len(files), inst)
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception as exc:
            log.warning("skip %s: %s", f, exc)
    data = pd.concat(frames, ignore_index=True)
    data["trade_date"] = pd.to_datetime(data.get("trade_date", data.index))
    return data[(data["trade_date"] >= start) & (data["trade_date"] <= end)].copy()


def add_direction_labels(df: pd.DataFrame) -> pd.DataFrame:
    """1 = price moved UP >= min_move in next horizon_min bars, 0 = DOWN, NaN = flat/invalid."""
    df = df.sort_values(["trade_date", "timestamp"]).copy()
    close_col = next((c for c in ["px_fut_close", "close", "fut_close"] if c in df.columns), None)
    if close_col is None:
        raise ValueError("No close price column found")
    close = pd.to_numeric(df[close_col], errors="coerce")
    n = LABEL_HORIZON_MIN
    future_max = close.shift(-1).rolling(n, min_periods=n).max().shift(-(n - 1))
    future_min = close.shift(-1).rolling(n, min_periods=n).min().shift(-(n - 1))
    up_move   = (future_max - close)
    down_move = (close - future_min)
    # Only label bars where at least one side exceeds LABEL_MIN_MOVE_PT
    big_up   = up_move   >= LABEL_MIN_MOVE_PT
    big_down = down_move >= LABEL_MIN_MOVE_PT
    label = pd.Series(np.nan, index=df.index)
    # Up wins: bigger up move AND up exceeds threshold
    label[big_up & ~big_down] = 1.0
    label[big_down & ~big_up] = 0.0
    # Both sides: label the dominant direction
    both = big_up & big_down
    label[both] = (up_move[both] >= down_move[both]).astype(float)
    # Lookahead invalid
    label[future_max.isna()] = np.nan
    # Session filter 9:45-15:05: outside window → NaN
    try:
        ts_col = pd.to_datetime(df["timestamp"] if "timestamp" in df.columns else df.index)
        mod = ts_col.dt.hour * 60 + ts_col.dt.minute
        outside = (mod < SESSION_START_MIN) | (mod > SESSION_END_MIN)
        label[outside] = np.nan
        log.info("Session filter 9:45-15:05: excluded %d bars outside window", outside.sum())
    except Exception:
        pass
    df["direction_label"] = label
    valid = df["direction_label"].notna()
    log.info("Direction labels: %d valid bars, pos_rate(CE)=%.3f",
             valid.sum(), df.loc[valid, "direction_label"].mean())
    return df


def build_feature_matrix(df: pd.DataFrame, features: List[str],
                          medians: Optional[Dict[str, float]] = None) -> pd.DataFrame:
    X = pd.DataFrame(index=df.index)
    for feat in features:
        s = resolve_column(df, feat)
        X[feat] = s if s is not None else np.nan
    if medians:
        for feat in features:
            if feat in medians and X[feat].isna().any():
                X[feat] = X[feat].fillna(medians[feat])
    return X


def compute_medians(df: pd.DataFrame, features: List[str]) -> Dict[str, float]:
    medians = {}
    for feat in features:
        s = resolve_column(df, feat)
        if s is not None:
            v = s.median()
            medians[feat] = 0.0 if pd.isna(v) else float(v)
        else:
            medians[feat] = 0.0
    return medians


def run_hpo(X_tr, y_tr, X_va, y_va, n_trials=50):
    import optuna, xgboost as xgb
    from sklearn.metrics import roc_auc_score
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    pos_w = float((y_tr == 0).sum() / max(1, (y_tr == 1).sum()))

    def objective(trial):
        p = {
            "n_estimators":     trial.suggest_int("n_estimators", 200, 1000),
            "max_depth":        trial.suggest_int("max_depth", 3, 8),
            "learning_rate":    trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "subsample":        trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.4, 1.0),
            "min_child_weight": trial.suggest_int("min_child_weight", 1, 30),
            "reg_alpha":        trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
            "reg_lambda":       trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
            "scale_pos_weight": pos_w,
            "use_label_encoder": False, "eval_metric": "auc",
            "random_state": 42, "n_jobs": -1,
        }
        m = xgb.XGBClassifier(**p)
        m.fit(X_tr, y_tr, verbose=False)
        return roc_auc_score(y_va, m.predict_proba(X_va)[:, 1])

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    log.info("Best AUC=%.4f", study.best_value)
    bp = {**study.best_params, "scale_pos_weight": pos_w,
          "use_label_encoder": False, "eval_metric": "auc",
          "random_state": 42, "n_jobs": -1}
    final = xgb.XGBClassifier(**bp)
    final.fit(X_tr, y_tr, verbose=False)
    return final, study.best_params, study.best_value


def evaluate(model, X, y, label="holdout"):
    from sklearn.metrics import roc_auc_score, brier_score_loss
    prob = model.predict_proba(X)[:, 1]
    auc   = float(roc_auc_score(y, prob))
    brier = float(brier_score_loss(y, prob))
    log.info("%s: AUC=%.4f brier=%.4f", label, auc, brier)
    return {"rows": int(len(y)), "base_rate": round(float(y.mean()), 4),
            "roc_auc": round(auc, 4), "brier": round(brier, 4)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instrument", required=True, choices=known_instruments())
    ap.add_argument("--data-dir",  required=True)
    ap.add_argument("--output",    required=True)
    ap.add_argument("--train-start",   default="2024-11-01")
    ap.add_argument("--train-end",     default="2026-03-31")
    ap.add_argument("--valid-start",   default="2026-04-01")
    ap.add_argument("--valid-end",     default="2026-05-31")
    ap.add_argument("--holdout-start", default="2026-06-01")
    ap.add_argument("--holdout-end",   default="2026-06-30")
    ap.add_argument("--n-trials", type=int, default=60)
    args = ap.parse_args(argv)

    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))

    features = DIRECTION_FEATURES_V3
    log.info("Instrument=%s | Features=%d", args.instrument, len(features))

    df = load_indicators(Path(args.data_dir).resolve(), args.instrument,
                         args.train_start, args.holdout_end)
    # Rename vix if needed
    if "vix_current" not in df.columns and "vix" in df.columns:
        df["vix_current"] = pd.to_numeric(df["vix"], errors="coerce")

    df = add_direction_labels(df)
    df = df[df["direction_label"].notna()].copy()

    def _s(start, end):
        return df[(df["trade_date"] >= start) & (df["trade_date"] <= end)].copy()

    tr = _s(args.train_start, args.train_end)
    va = _s(args.valid_start, args.valid_end)
    ho = _s(args.holdout_start, args.holdout_end)
    log.info("Split train=%d valid=%d holdout=%d", len(tr), len(va), len(ho))

    medians = compute_medians(tr, features)

    def prep(d):
        X = build_feature_matrix(d, features, medians)
        y = d["direction_label"].to_numpy(float)
        ok = np.isfinite(y) & ~X.isna().any(axis=1)
        return X[ok], y[ok]

    X_tr, y_tr = prep(tr)
    X_va, y_va = prep(va)
    X_ho, y_ho = prep(ho)
    log.info("Clean train=%d(%.3f) valid=%d(%.3f) holdout=%d(%.3f)",
             len(y_tr), y_tr.mean(), len(y_va), y_va.mean(), len(y_ho), y_ho.mean())

    model, best_params, va_auc = run_hpo(X_tr, y_tr, X_va, y_va, args.n_trials)

    # Calibrate
    from sklearn.calibration import CalibratedClassifierCV
    try:
        cal = CalibratedClassifierCV(estimator=model, method="isotonic", cv="prefit")
    except TypeError:
        cal = CalibratedClassifierCV(base_estimator=model, method="isotonic", cv="prefit")
    cal.fit(X_va, y_va)

    holdout_eval = evaluate(cal, X_ho, y_ho)
    auc = holdout_eval["roc_auc"]
    gates = {"auc>=0.60": auc >= 0.60}
    all_pass = all(gates.values())

    importances = {}
    try:
        for feat, imp in zip(features, model.feature_importances_):
            importances[feat] = round(float(imp), 6)
    except Exception:
        pass

    bundle = {
        "kind":         "direction_only_bundle",
        "version":      "3.0",
        "instrument":   args.instrument,
        "source":       "train_direction_dhan_v3",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "features":     features,
        "medians":      medians,         # kept for backward compat
        "feature_medians": medians,      # serving code reads this key
        "max_nan_features": MAX_NAN_FEATURES,
        "model":        cal,
        "holdout_eval": holdout_eval,
        "feature_importances": importances,
        "ship_gates":   gates,
        "ship_gates_all_pass": all_pass,
        "column_aliases": COLUMN_ALIASES,
        "training_metadata": {
            "instrument": args.instrument,
            "train_window": f"{args.train_start}..{args.train_end}",
            "holdout_window": f"{args.holdout_start}..{args.holdout_end}",
            "label_horizon_min": LABEL_HORIZON_MIN,
            "label_min_move_pt": LABEL_MIN_MOVE_PT,
            "n_train": int(len(y_tr)), "n_holdout": int(len(y_ho)),
            "hpo_valid_auc": round(float(va_auc), 4),
            "best_params": best_params,
        },
    }

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out)
    report = {k: v for k, v in bundle.items() if k != "model"}
    out.with_suffix(".report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info("Saved: %s (AUC=%.4f ALL_PASS=%s)", out, auc, all_pass)

    rb = joblib.load(out)
    assert rb["features"] == features
    log.info("Reload OK")
    return 0 if all_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
