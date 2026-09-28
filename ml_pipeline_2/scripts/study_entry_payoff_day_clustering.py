"""Day-level re-evaluation of the entry-timing payoff model
(train_entry_option_payoff_v1) -- is its ranking skill a real, predictive
signal, or mostly a same-day volatility detector carried by a few days?

The original ship-gate numbers (NIFTY holdout rho=0.154, BankNifty 0.218)
treat ~14k holdout bars as independent observations. Bars within a day are
autocorrelated, so this study re-tests everything with the DAY as the unit:

  A. Fixed holdout (2026-06-01..07-31) scored by the ACTUAL deployed-research
     model bundles (``--model-dir``; pulled from trader-runtime-01).
  B. Walk-forward: ``conditional_direction.build_expanding_windows``
     (5 windows, min_train_days=200 -- same split as every other track). Per
     window the model is retrained with no leakage: the window's training
     days minus the last ``--inner-valid-days`` are the fit set, those last
     days are the validation set (for HPO and for the fixed first-fire
     threshold), and the window's test days are never seen.

For each scored frame it reports (see
``ml_pipeline_2.pipeline.entry_payoff_day_clustering``): top-decile day
concentration, day-cluster bootstrap CIs, day-level and within-day ranking
skill, top-day-removal robustness, an early-session ("does the score by
10:15 / 10:45 predict the rest of the day?") test against simple
volatility/calendar baselines, and the first-fire-per-day P&L at a
threshold fixed in advance.

Usage:
    python -m ml_pipeline_2.scripts.study_entry_payoff_day_clustering \\
        --instruments nifty,banknifty \\
        --model-dir <dir with {inst}_entry_option_payoff_v1.joblib> \\
        --mode both --hpo-trials 15 \\
        --output-dir .run/entry_payoff_day_clustering
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("study_entry_payoff_day_clustering")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.conditional_direction import WalkForwardWindow, build_expanding_windows  # noqa: E402
from ml_pipeline_2.pipeline.entry_payoff_day_clustering import (  # noqa: E402
    aggregate_by_day,
    assign_rank_buckets,
    between_day_variance_share,
    bootstrap_mean,
    cluster_bootstrap,
    day_concentration,
    day_mean_score_rho,
    decile_spread,
    demean_by_day,
    early_session_day_frame,
    first_fire_per_day,
    partial_spearman,
    robustness_to_top_days,
    side_agnostic_bucket_table,
    spearman,
    verify_side_returns,
    spearman_with_p,
    within_day_bucket_spread,
    within_day_spearman,
)
from ml_pipeline_2.pipeline.entry_quality_direction import (  # noqa: E402
    DEFAULT_XGB_REGRESSOR_PARAMS,
    fit_entry_payoff_model,
    score_entry_payoff,
)
from ml_pipeline_2.scripts.train_entry_dhan_v3 import ENTRY_FEATURES_V3  # noqa: E402

TARGET_COL = "payoff_score"
SCORE_COL = "score"
DEFAULT_N_WINDOWS = 5
DEFAULT_MIN_TRAIN_DAYS = 200
DEFAULT_INNER_VALID_DAYS = 21
DEFAULT_CUTOFFS = ("10:15:00", "10:45:00")
HOLDOUT_START, HOLDOUT_END = "2026-06-01", "2026-07-31"
VALID_START, VALID_END = "2026-05-01", "2026-05-31"
FIRE_QUANTILE = 0.90

# Simple, causally-available "is it volatile / is it expiry" rankers the
# model must beat to be more than a volatility detector. None of these is in
# ENTRY_FEATURES_V3 except vix_current.
BASELINE_RANKERS: Tuple[str, ...] = ("atr_14_1m", "range_30", "bb_width_20", "vix_current", "neg_dte")
EARLY_MEAN_COLS: Tuple[str, ...] = ("atr_14_1m",)
AT_CUTOFF_COLS: Tuple[str, ...] = ("range_30", "vix_current", "neg_dte", "abs_move_from_open", "prior_day_mean_payoff")
PARTIAL_CONTROLS: Tuple[str, ...] = (
    "early_mean_atr_14_1m", "at_cutoff_vix_current", "at_cutoff_neg_dte", "at_cutoff_prior_day_mean_payoff",
)


# ── Data prep ───────────────────────────────────────────────────────────────

def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add the baseline helper columns: ``neg_dte`` (so larger = nearer
    expiry, same orientation as the other rankers), ``abs_move_from_open``
    and ``prior_day_mean_payoff`` (previous TRADING day's mean realized
    payoff across all its bars -- fully known before today's open)."""
    out = df.copy()
    out["neg_dte"] = -pd.to_numeric(out["dte"], errors="coerce")
    out["abs_move_from_open"] = pd.to_numeric(out["vel_price_delta_open"], errors="coerce").abs()
    day_mean = out.groupby("trade_date")[TARGET_COL].mean().sort_index()
    out["prior_day_mean_payoff"] = out["trade_date"].map(day_mean.shift(1))
    return out


SIDE_COLS = ("net_ce_return", "net_pe_return")
# Per-side net returns at the SAME 15-bar horizon as payoff_score (verified
# 2026-09-28: max(ce, pe) == payoff_score on every matched row). NOTE the
# BankNifty base `_direction_margin` file is a different horizon -- do not use.
DEFAULT_SIDE_RETURNS = {
    "nifty": ".run/training_view/training_view_nifty_direction_margin.csv.gz",
    "banknifty": ".run/training_view/training_view_banknifty_direction_margin_h15.csv.gz",
}


def attach_side_returns(df: pd.DataFrame, side_path: str) -> pd.DataFrame:
    """Left-join net_ce_return/net_pe_return on (trade_date, time) and
    verify they reproduce payoff_score (raises on a horizon mismatch)."""
    side = pd.read_csv(side_path, usecols=["trade_date", "time", *SIDE_COLS])
    side["trade_date"] = side["trade_date"].astype(str)
    side["time"] = side["time"].astype(str)
    out = df.merge(side.drop_duplicates(["trade_date", "time"]), on=["trade_date", "time"], how="left")
    bad = verify_side_returns(out[TARGET_COL].to_numpy(), out[SIDE_COLS[0]].to_numpy(), out[SIDE_COLS[1]].to_numpy())
    log.info("attached side returns from %s: coverage=%.4f, mismatch=%.5f", side_path, out[SIDE_COLS[0]].notna().mean(), bad)
    return out


def load_frame(path: str, side_returns_path: Optional[str] = None) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["trade_date"] = df["trade_date"].astype(str)
    df["time"] = df["time"].astype(str)
    df[TARGET_COL] = pd.to_numeric(df[TARGET_COL], errors="coerce")
    df = df.dropna(subset=[TARGET_COL]).sort_values(["trade_date", "time"]).reset_index(drop=True)
    if side_returns_path:
        df = attach_side_returns(df, side_returns_path)
    return add_derived_columns(df)


def split_inner_valid(train_df: pd.DataFrame, inner_valid_days: int) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Last ``inner_valid_days`` trading days of a window's training span
    become its validation set (HPO + first-fire threshold); the rest is the
    fit set. Mirrors the original train / valid(May) / holdout layout."""
    days = sorted(train_df["trade_date"].unique())
    if len(days) <= inner_valid_days:
        raise ValueError(f"window has only {len(days)} train days, need > inner_valid_days={inner_valid_days}")
    cut = days[-inner_valid_days]
    return train_df[train_df["trade_date"] < cut].reset_index(drop=True), train_df[train_df["trade_date"] >= cut].reset_index(drop=True)


# ── Analysis of one scored frame ────────────────────────────────────────────

def _r(x: float, n: int = 5) -> Optional[float]:
    return None if x is None or not np.isfinite(x) else round(float(x), n)


def _rho_stat(score_col: str) -> Any:
    return lambda f: spearman(f[score_col].to_numpy(), f[TARGET_COL].to_numpy())


def _spread_stat(score_col: str) -> Any:
    return lambda f: decile_spread(f[score_col].to_numpy(), f[TARGET_COL].to_numpy())[2]


def _top_mean_stat(score_col: str) -> Any:
    return lambda f: decile_spread(f[score_col].to_numpy(), f[TARGET_COL].to_numpy())[0]


def _fixed_top_mean_stat(fire_col: str) -> Any:
    # Top-decile mean with fire membership FIXED from the full sample --
    # the bootstrap then measures only which days are in the sample.
    return lambda f: float(f.loc[f[fire_col], TARGET_COL].mean()) if f[fire_col].any() else float("nan")


def ranker_summary(df: pd.DataFrame, col: str) -> Dict[str, Any]:
    s = pd.to_numeric(df[col], errors="coerce").to_numpy()
    ok = np.isfinite(s)
    sub = df[ok].assign(_s=s[ok])
    top, bot, spread = decile_spread(sub["_s"].to_numpy(), sub[TARGET_COL].to_numpy())
    wd = within_day_spearman(sub, score_col="_s")
    return {
        "coverage": _r(float(ok.mean()), 4),
        "bar_rho": _r(spearman(sub["_s"].to_numpy(), sub[TARGET_COL].to_numpy()), 4),
        "top_mean": _r(top), "bottom_mean": _r(bot), "spread": _r(spread),
        "within_day_mean_rho": _r(float(wd.mean()) if len(wd) else float("nan"), 4),
    }


def early_session_tests(
    df: pd.DataFrame, *, score_col: str, cutoff: str, n_boot: int, seed: int,
) -> Dict[str, Any]:
    ef = early_session_day_frame(
        df, cutoff=cutoff, score_col=score_col, payoff_col=TARGET_COL,
        early_mean_cols=EARLY_MEAN_COLS, at_cutoff_cols=AT_CUTOFF_COLS,
    )
    y = ef["later_mean_payoff"].to_numpy()
    rho, p = spearman_with_p(ef["early_mean_score"].to_numpy(), y)
    ci = cluster_bootstrap(
        ef, lambda f: spearman(f["early_mean_score"].to_numpy(), f["later_mean_payoff"].to_numpy()),
        n_boot=n_boot, seed=seed,
    )
    rho_max, p_max = spearman_with_p(ef["early_max_score"].to_numpy(), y)
    baselines: Dict[str, Any] = {}
    for c in [f"early_mean_{c}" for c in EARLY_MEAN_COLS] + [f"at_cutoff_{c}" for c in AT_CUTOFF_COLS]:
        br, bp = spearman_with_p(ef[c].to_numpy(), y)
        baselines[c] = {"rho": _r(br, 4), "p": _r(bp, 5)}
    controls = [c for c in PARTIAL_CONTROLS if c in ef.columns]
    pr_dte = partial_spearman(ef["early_mean_score"].to_numpy(), y, ef["at_cutoff_neg_dte"].to_numpy())
    pr = partial_spearman(ef["early_mean_score"].to_numpy(), y, ef[controls].to_numpy())
    pr_ci = cluster_bootstrap(
        ef, lambda f: partial_spearman(f["early_mean_score"].to_numpy(), f["later_mean_payoff"].to_numpy(), f[controls].to_numpy()),
        n_boot=n_boot, seed=seed + 1,
    )
    # Does a top-tercile early score day actually pay more later? (tradeable framing)
    q_hi = ef["early_mean_score"].quantile(2 / 3)
    q_lo = ef["early_mean_score"].quantile(1 / 3)
    hi_days = ef[ef["early_mean_score"] >= q_hi]["later_mean_payoff"]
    lo_days = ef[ef["early_mean_score"] <= q_lo]["later_mean_payoff"]
    return {
        "cutoff": cutoff,
        "n_days": int(len(ef)),
        "rho_early_mean_score_vs_later_payoff": _r(rho, 4),
        "p": _r(p, 5),
        "cluster_ci": ci.to_dict(),
        "rho_early_max_score_vs_later_payoff": _r(rho_max, 4), "p_max": _r(p_max, 5),
        "rho_early_score_vs_later_score": _r(spearman(ef["early_mean_score"].to_numpy(), ef["later_mean_score"].to_numpy()), 4),
        "baselines": baselines,
        "partial_rho_given_dte_only": _r(pr_dte, 4),
        "partial_rho_controls": controls,
        "partial_rho": _r(pr, 4),
        "partial_rho_ci": pr_ci.to_dict(),
        "later_payoff_top_tercile_early_score_days": _r(float(hi_days.mean())),
        "later_payoff_bottom_tercile_early_score_days": _r(float(lo_days.mean())),
        "n_tercile_days": [int(len(hi_days)), int(len(lo_days))],
    }


def analyze_scored_frame(
    df: pd.DataFrame,
    *,
    score_col: str = SCORE_COL,
    n_boot: int = 2000,
    seed: int = 0,
    cutoffs: Sequence[str] = DEFAULT_CUTOFFS,
    fire_threshold: Optional[float] = None,
    include_baselines: bool = True,
) -> Dict[str, Any]:
    """Full day-level diagnostic suite for one scored frame (must carry
    trade_date, time, payoff_score, ``score_col`` and the derived baseline
    columns)."""
    df = df.reset_index(drop=True).copy()
    s = df[score_col].to_numpy()
    y = df[TARGET_COL].to_numpy()
    buckets = assign_rank_buckets(s)
    df["_fire"] = buckets == buckets.max()
    df["_bottom"] = buckets == 1

    rho, p = spearman_with_p(s, y)
    top, bot, spread = decile_spread(s, y)
    res: Dict[str, Any] = {
        "bar_level": {
            "n_rows": int(len(df)), "n_days": int(df["trade_date"].nunique()),
            "spearman_rho": _r(rho, 4), "spearman_p_naive_iid_bars": p,
            "top_decile_mean": _r(top), "bottom_decile_mean": _r(bot), "spread": _r(spread),
            "all_bars_mean": _r(float(y.mean())),
        },
    }
    fires = df[df["_fire"]]
    res["concentration"] = day_concentration(fires["trade_date"].to_numpy(), fires[TARGET_COL].to_numpy()).to_dict()
    bottoms = df[df["_bottom"]]
    res["bottom_decile_concentration"] = {
        "n_days": int(bottoms["trade_date"].nunique()),
        "fires_per_day_max": int(bottoms["trade_date"].value_counts().max()) if len(bottoms) else 0,
    }

    core = df[["trade_date", score_col, TARGET_COL, "_fire", "neg_dte"]]
    res["cluster_bootstrap"] = {
        "spearman_rho": cluster_bootstrap(core, _rho_stat(score_col), n_boot=n_boot, seed=seed).to_dict(),
        "decile_spread": cluster_bootstrap(core, _spread_stat(score_col), n_boot=n_boot, seed=seed + 1).to_dict(),
        "top_decile_mean": cluster_bootstrap(core, _top_mean_stat(score_col), n_boot=n_boot, seed=seed + 2).to_dict(),
        "top_decile_mean_fixed_membership": cluster_bootstrap(core, _fixed_top_mean_stat("_fire"), n_boot=n_boot, seed=seed + 3).to_dict(),
    }

    # Expiry-calendar control: days-to-expiry is known in advance and is NOT
    # a model feature -- does the model add anything beyond it?
    dte_rho = within_day_spearman(df, date_col="neg_dte", score_col=score_col, min_bars=200)
    dte_sizes = df.groupby("neg_dte").size().reindex(dte_rho.index)
    res["dte_control"] = {
        "bar_partial_rho_given_dte": _r(partial_spearman(s, y, df["neg_dte"].to_numpy()), 4),
        "bar_partial_rho_given_dte_ci": cluster_bootstrap(
            core, lambda f: partial_spearman(f[score_col].to_numpy(), f[TARGET_COL].to_numpy(), f["neg_dte"].to_numpy()),
            n_boot=n_boot, seed=seed + 8,
        ).to_dict(),
        "within_dte_group_rho_weighted": _r(float(np.average(dte_rho, weights=dte_sizes)) if len(dte_rho) else float("nan"), 4),
        "within_dte_group_rho": {str(-k): _r(v, 4) for k, v in dte_rho.items()},
        "mean_payoff_by_dte": {str(-k): _r(v) for k, v in df.groupby("neg_dte")[TARGET_COL].mean().items()},
        "n_days_by_dte": {str(-k): int(v) for k, v in df.groupby("neg_dte")["trade_date"].nunique().items()},
        "fires_by_dte": {str(-k): int(v) for k, v in df[df["_fire"]].groupby("neg_dte").size().items()},
    }

    # Between- vs within-day decomposition.
    dm = demean_by_day(df, [score_col, TARGET_COL])
    res["decomposition"] = {
        "payoff_between_day_variance_share": _r(between_day_variance_share(df["trade_date"].to_numpy(), y), 4),
        "score_between_day_variance_share": _r(between_day_variance_share(df["trade_date"].to_numpy(), s), 4),
        "rho_if_score_replaced_by_day_mean_score": _r(day_mean_score_rho(df, score_col=score_col), 4),
        "rho_day_demeaned_pooled": _r(spearman(dm[score_col].to_numpy(), dm[TARGET_COL].to_numpy()), 4),
    }

    # Day level: one observation per day.
    day = aggregate_by_day(df, score_col=score_col, payoff_col=TARGET_COL, fire_col="_fire")
    drho, dp = spearman_with_p(day["mean_score"].to_numpy(), day["mean_payoff"].to_numpy())
    dci = cluster_bootstrap(
        day, lambda f: spearman(f["mean_score"].to_numpy(), f["mean_payoff"].to_numpy()), n_boot=n_boot, seed=seed + 4,
    )
    fire_days = day[day["n_fires"] > 0]
    paired = (fire_days["fire_mean_payoff"] - fire_days["nonfire_mean_payoff"]).dropna().to_numpy()
    res["day_level"] = {
        "n_days": int(len(day)),
        "rho_day_mean_score_vs_day_mean_payoff": _r(drho, 4), "p": _r(dp, 5),
        "cluster_ci": dci.to_dict(),
        "mean_payoff_fire_days": _r(float(fire_days["mean_payoff"].mean())),
        "mean_payoff_nonfire_days": _r(float(day[day["n_fires"] == 0]["mean_payoff"].mean())),
        "fire_minus_nonfire_same_day": bootstrap_mean(paired, n_boot=n_boot, seed=seed + 5).to_dict(),
        "n_fire_days_with_nonfire_bars": int(len(paired)),
    }

    # Within day: which bar.
    wd_rho = within_day_spearman(df, score_col=score_col)
    wd_spread = within_day_bucket_spread(df, score_col=score_col)
    res["within_day"] = {
        "n_days": int(len(wd_rho)),
        "mean_per_day_rho": bootstrap_mean(wd_rho.to_numpy(), n_boot=n_boot, seed=seed + 6).to_dict(),
        "frac_days_rho_positive": _r(float((wd_rho > 0).mean()), 4),
        "mean_per_day_decile_spread": bootstrap_mean(wd_spread.to_numpy(), n_boot=n_boot, seed=seed + 7).to_dict(),
        "frac_days_spread_positive": _r(float((wd_spread > 0).mean()), 4),
    }

    res["top_day_removal"] = robustness_to_top_days(df, score_col=score_col, payoff_col=TARGET_COL)
    res["early_session"] = [
        early_session_tests(df, score_col=score_col, cutoff=c, n_boot=n_boot, seed=seed + 10 + i)
        for i, c in enumerate(cutoffs)
    ]
    if include_baselines:
        res["baseline_rankers"] = {c: ranker_summary(df, c) for c in BASELINE_RANKERS if c in df.columns}
        res["baseline_rankers"]["model_score"] = ranker_summary(df, score_col)

    if all(c in df.columns for c in SIDE_COLS):
        res["side_agnostic"] = side_agnostic_summary(df, score_col=score_col, n_boot=n_boot, seed=seed + 30,
                                                     fire_threshold=fire_threshold)

    if fire_threshold is not None:
        ff = first_fire_per_day(df, threshold=fire_threshold, score_col=score_col, payoff_col=TARGET_COL)
        res["first_fire_per_day"] = summarize_first_fires(ff, all_days_mean=float(aggregate_by_day(df, score_col=score_col)["mean_payoff"].mean()),
                                                          n_days_total=int(df["trade_date"].nunique()), threshold=fire_threshold,
                                                          n_boot=n_boot, seed=seed + 20)
    return res


def side_agnostic_summary(
    df: pd.DataFrame, *, score_col: str, n_boot: int, seed: int, fire_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    """What the top decile earns WITHOUT hindsight direction: coin-flip side
    = (net_ce + net_pe) / 2, with a day-cluster CI on the top decile."""
    d = df.dropna(subset=list(SIDE_COLS)).copy()
    d["_coin"] = (d[SIDE_COLS[0]] + d[SIDE_COLS[1]]) / 2
    b = assign_rank_buckets(d[score_col].to_numpy())
    d["_top"] = b == b.max()
    core = d[["trade_date", "_coin", "_top"]]
    top_ci = cluster_bootstrap(
        core, lambda f: float(f.loc[f["_top"], "_coin"].mean()) if f["_top"].any() else float("nan"),
        n_boot=n_boot, seed=seed,
    )
    out: Dict[str, Any] = {
        "bucket_table": side_agnostic_bucket_table(d[score_col].to_numpy(), d[SIDE_COLS[0]].to_numpy(), d[SIDE_COLS[1]].to_numpy()),
        "all_bars_coin_flip_mean": _r(float(d["_coin"].mean())),
        "top_decile_coin_flip_mean_ci": top_ci.to_dict(),
        "rho_score_vs_coin_flip": _r(spearman(d[score_col].to_numpy(), d["_coin"].to_numpy()), 4),
    }
    if fire_threshold is not None:
        ff = first_fire_per_day(d, threshold=fire_threshold, score_col=score_col, payoff_col="_coin")
        out["first_fire_coin_flip_mean"] = bootstrap_mean(ff["_coin"].to_numpy(), n_boot=n_boot, seed=seed + 1).to_dict() if len(ff) else None
    return out


def summarize_first_fires(
    ff: pd.DataFrame, *, all_days_mean: float, n_days_total: int, threshold: Optional[float], n_boot: int, seed: int,
) -> Dict[str, Any]:
    if ff.empty:
        return {"threshold": threshold, "n_days_fired": 0, "n_days_total": n_days_total}
    diff = (ff[TARGET_COL] - ff["day_mean_payoff"]).to_numpy()
    return {
        "threshold": _r(threshold, 6) if threshold is not None else None,
        "n_days_fired": int(len(ff)), "n_days_total": n_days_total,
        "median_first_fire_time": str(sorted(ff["time"])[len(ff) // 2]),
        "first_fire_mean_payoff": bootstrap_mean(ff[TARGET_COL].to_numpy(), n_boot=n_boot, seed=seed).to_dict(),
        "first_fire_win_rate": _r(float((ff[TARGET_COL] > 0).mean()), 4),
        "same_day_all_bars_mean_payoff": _r(float(ff["day_mean_payoff"].mean())),
        "first_fire_minus_same_day_mean": bootstrap_mean(diff, n_boot=n_boot, seed=seed + 1).to_dict(),
        "all_days_mean_payoff": _r(all_days_mean),
    }


# ── Model fitting per window ────────────────────────────────────────────────

@dataclass
class FittedWindowModel:
    model: Any
    medians: pd.Series
    params: Dict[str, Any]
    valid_rho: float
    fire_threshold: float


def fit_window_model(
    fit_df: pd.DataFrame, valid_df: pd.DataFrame, features: Sequence[str], *, hpo_trials: int,
) -> FittedWindowModel:
    """HPO path reuses train_entry_option_payoff_v1.run_hpo (validation
    Spearman objective, winsorized training target, medians from the fit set
    only); ``hpo_trials=0`` uses entry_quality_direction's fixed default
    regressor params instead."""
    if hpo_trials > 0:
        from ml_pipeline_2.scripts.train_entry_option_payoff_v1 import run_hpo, winsorize_target

        X_fit = fit_df[list(features)].apply(pd.to_numeric, errors="coerce")
        medians = X_fit.median()
        X_fit = X_fit.fillna(medians).fillna(0.0)
        X_val = valid_df[list(features)].apply(pd.to_numeric, errors="coerce").fillna(medians).fillna(0.0)
        y_fit, _, _ = winsorize_target(fit_df[TARGET_COL].to_numpy())
        model, params, _ = run_hpo(X_fit, y_fit, X_val, valid_df[TARGET_COL].to_numpy(), n_trials=hpo_trials)
    else:
        model, medians = fit_entry_payoff_model(fit_df, features, TARGET_COL)
        params = dict(DEFAULT_XGB_REGRESSOR_PARAMS)
    vpred = score_entry_payoff(model, medians, valid_df, features)
    return FittedWindowModel(
        model=model, medians=medians, params=params,
        valid_rho=spearman(vpred, valid_df[TARGET_COL].to_numpy()),
        fire_threshold=float(np.quantile(vpred, FIRE_QUANTILE)),
    )


def select_window(df: pd.DataFrame, w: WalkForwardWindow) -> Tuple[pd.DataFrame, pd.DataFrame]:
    tr = df[(df["trade_date"] >= w.train_start) & (df["trade_date"] <= w.train_end)].reset_index(drop=True)
    te = df[(df["trade_date"] >= w.test_start) & (df["trade_date"] <= w.test_end)].reset_index(drop=True)
    return tr, te


# ── Drivers ─────────────────────────────────────────────────────────────────

def _save_scored(df: pd.DataFrame, path: Path) -> None:
    cols = [c for c in ("trade_date", "time", "window_id", SCORE_COL, "score_pct", TARGET_COL, *SIDE_COLS, "dte") if c in df.columns]
    df[cols].to_csv(path, index=False)
    log.info("wrote scored frame %s (%d rows)", path, len(df))


def run_holdout(df: pd.DataFrame, bundle: Dict[str, Any], *, n_boot: int, scored_out: Optional[Path] = None) -> Dict[str, Any]:
    features = list(bundle["features"])
    medians = pd.Series(bundle["feature_medians"])
    hold = df[(df["trade_date"] >= HOLDOUT_START) & (df["trade_date"] <= HOLDOUT_END)].reset_index(drop=True)
    valid = df[(df["trade_date"] >= VALID_START) & (df["trade_date"] <= VALID_END)].reset_index(drop=True)
    hold[SCORE_COL] = score_entry_payoff(bundle["model"], medians, hold, features)
    vpred = score_entry_payoff(bundle["model"], medians, valid, features)
    thr = float(np.quantile(vpred, FIRE_QUANTILE))
    log.info("holdout: %d rows / %d days, first-fire threshold (valid p90)=%.5f", len(hold), hold["trade_date"].nunique(), thr)
    if scored_out is not None:
        _save_scored(hold, scored_out)
    out = analyze_scored_frame(hold, n_boot=n_boot, fire_threshold=thr)
    out["reported_holdout_eval"] = {k: bundle["holdout_eval"].get(k) for k in ("spearman_rho", "top_decile_mean_payoff", "bottom_decile_mean_payoff", "rows")}
    return out


def run_walkforward(
    df: pd.DataFrame, *, features: Sequence[str], n_windows: int, min_train_days: int,
    inner_valid_days: int, hpo_trials: int, n_boot: int, scored_out: Optional[Path] = None,
) -> Dict[str, Any]:
    windows = build_expanding_windows(sorted(df["trade_date"].unique().tolist()), n_windows=n_windows, min_train_days=min_train_days)
    per_window: List[Dict[str, Any]] = []
    scored_tests: List[pd.DataFrame] = []
    first_fires: List[pd.DataFrame] = []
    for w in windows:
        tr, te = select_window(df, w)
        fit_df, valid_df = split_inner_valid(tr, inner_valid_days)
        log.info("window %d: fit %s..%s (%d rows), valid %s..%s, test %s..%s (%d rows / %d days)",
                 w.window_id, fit_df["trade_date"].min(), fit_df["trade_date"].max(), len(fit_df),
                 valid_df["trade_date"].min(), valid_df["trade_date"].max(), w.test_start, w.test_end,
                 len(te), te["trade_date"].nunique())
        fm = fit_window_model(fit_df, valid_df, features, hpo_trials=hpo_trials)
        te = te.copy()
        te[SCORE_COL] = score_entry_payoff(fm.model, fm.medians, te, features)
        te["window_id"] = w.window_id
        te["score_pct"] = te[SCORE_COL].rank(pct=True)
        res = analyze_scored_frame(te, n_boot=n_boot, seed=w.window_id * 100, fire_threshold=fm.fire_threshold,
                                   include_baselines=True)
        res["window"] = {"window_id": w.window_id, "train_start": w.train_start, "fit_end": str(fit_df["trade_date"].max()),
                         "valid_start": str(valid_df["trade_date"].min()), "valid_end": str(valid_df["trade_date"].max()),
                         "test_start": w.test_start, "test_end": w.test_end, "valid_rho": _r(fm.valid_rho, 4),
                         "params": {k: (round(v, 5) if isinstance(v, float) else v) for k, v in fm.params.items()}}
        log.info("window %d: bar rho=%.4f CI[%s,%s] day rho=%s within-day rho=%s early10:15 rho=%s",
                 w.window_id, res["bar_level"]["spearman_rho"], res["cluster_bootstrap"]["spearman_rho"]["ci_low"],
                 res["cluster_bootstrap"]["spearman_rho"]["ci_high"], res["day_level"]["rho_day_mean_score_vs_day_mean_payoff"],
                 res["within_day"]["mean_per_day_rho"]["point"], res["early_session"][0]["rho_early_mean_score_vs_later_payoff"])
        per_window.append(res)
        scored_tests.append(te)
        first_fires.append(first_fire_per_day(te, threshold=fm.fire_threshold, score_col=SCORE_COL, payoff_col=TARGET_COL))

    pooled_df = pd.concat(scored_tests, ignore_index=True)
    if scored_out is not None:
        _save_scored(pooled_df, scored_out)
    pooled = analyze_scored_frame(pooled_df, score_col="score_pct", n_boot=n_boot, seed=7, fire_threshold=None)
    ff = pd.concat(first_fires, ignore_index=True)
    pooled["first_fire_per_day"] = summarize_first_fires(
        ff, all_days_mean=float(pooled_df.groupby("trade_date")[TARGET_COL].mean().mean()),
        n_days_total=int(pooled_df["trade_date"].nunique()), threshold=None, n_boot=n_boot, seed=77,
    )
    return {"windows": per_window, "pooled": pooled,
            "config": {"n_windows": n_windows, "min_train_days": min_train_days,
                       "inner_valid_days": inner_valid_days, "hpo_trials": hpo_trials}}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instruments", default="nifty,banknifty")
    ap.add_argument("--training-view-template", default=".run/training_view/training_view_{inst}_option_pnl.csv.gz")
    ap.add_argument("--model-dir", default="models", help="dir holding {inst}_entry_option_payoff_v1.joblib (holdout mode)")
    ap.add_argument("--mode", choices=("holdout", "walkforward", "both"), default="both")
    ap.add_argument("--n-windows", type=int, default=DEFAULT_N_WINDOWS)
    ap.add_argument("--min-train-days", type=int, default=DEFAULT_MIN_TRAIN_DAYS)
    ap.add_argument("--inner-valid-days", type=int, default=DEFAULT_INNER_VALID_DAYS)
    ap.add_argument("--hpo-trials", type=int, default=15, help="0 = fixed default XGB params (fast)")
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--output-dir", default=".run/entry_payoff_day_clustering")
    ap.add_argument("--tag", default="", help="suffix for output filenames")
    ap.add_argument("--side-returns", default=",".join(f"{k}={v}" for k, v in DEFAULT_SIDE_RETURNS.items()),
                    help="inst=path,... per-side net-return views at the 15-bar horizon ('' disables)")
    args = ap.parse_args(argv)

    import joblib

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for inst in [s.strip() for s in args.instruments.split(",") if s.strip()]:
        side_map = dict(kv.split("=", 1) for kv in args.side_returns.split(",") if "=" in kv)
        df = load_frame(args.training_view_template.format(inst=inst), side_map.get(inst))
        suffix = f"_{args.tag}" if args.tag else ""
        log.info("%s: %d rows, %d days (%s..%s)", inst, len(df), df["trade_date"].nunique(), df["trade_date"].min(), df["trade_date"].max())
        result: Dict[str, Any] = {"instrument": inst}
        if args.mode in ("holdout", "both"):
            bundle = joblib.load(Path(args.model_dir) / f"{inst}_entry_option_payoff_v1.joblib")
            result["holdout"] = run_holdout(df, bundle, n_boot=args.n_boot,
                                            scored_out=out_dir / f"{inst}_holdout_scored{suffix}.csv.gz")
        if args.mode in ("walkforward", "both"):
            result["walkforward"] = run_walkforward(
                df, features=list(ENTRY_FEATURES_V3), n_windows=args.n_windows, min_train_days=args.min_train_days,
                inner_valid_days=args.inner_valid_days, hpo_trials=args.hpo_trials, n_boot=args.n_boot,
                scored_out=out_dir / f"{inst}_walkforward_scored{suffix}.csv.gz",
            )
        path = out_dir / f"{inst}_{args.mode}{suffix}.json"
        with open(path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info("wrote %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
