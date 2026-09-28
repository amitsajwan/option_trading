"""Day-level (cluster-aware) re-evaluation of the entry-timing payoff model
(2026-09-28).

``ml_pipeline_2.scripts.train_entry_option_payoff_v1`` predicts, per
1-minute bar, ``payoff_score = max(net CE return, net PE return)`` over a
15-bar hold, and its reported holdout significance (Spearman rho, top- vs
bottom-decile mean payoff) treats every bar as an independent observation.
Bars inside one trading day are strongly autocorrelated (a volatile day
produces many high-payoff bars at once), so that significance can be badly
overstated: the effective sample is closer to the number of distinct DAYS
than the number of bars.

This module holds the pure, model-free statistics used to re-test the
signal at the day level (see
``ml_pipeline_2.scripts.study_entry_payoff_day_clustering`` for the driver):

1. Rank buckets / decile spread (``assign_rank_buckets``, ``decile_spread``)
   -- bucket assignment identical to
   ``train_entry_option_payoff_v1.decile_lift_table`` (argsort, equal-size
   ``np.linspace`` edges), so "top decile" here means exactly what it meant
   in the original ship-gate report.
2. Fire concentration by day (``day_concentration``,
   ``top_contributing_days``).
3. Cluster (block-by-day) bootstrap (``cluster_bootstrap``) -- resamples
   whole days with replacement, relabelling duplicated days so within-day
   statistics still see them as distinct clusters.
4. Day aggregation and within-day decomposition (``aggregate_by_day``,
   ``within_day_spearman``, ``within_day_bucket_spread``, ``demean_by_day``,
   ``between_day_variance_share``).
5. Robustness to dropping top days (``robustness_to_top_days``).
6. Early-session "does it call the day in advance" frame
   (``early_session_day_frame``, ``partial_spearman``).
7. Fixed-threshold first-fire-per-day (``first_fire_per_day``) -- the
   one-trade-per-day view a real trader would actually take.

Pure numpy/pandas/scipy only: no model fitting, no ``strategy_app`` import.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_N_BUCKETS = 10
DEFAULT_N_BOOT = 2000
DEFAULT_ALPHA = 0.05


# ── Rank buckets ────────────────────────────────────────────────────────────

def assign_rank_buckets(scores: np.ndarray, *, n_buckets: int = DEFAULT_N_BUCKETS) -> np.ndarray:
    """Bucket index (1 = lowest score, ``n_buckets`` = highest) for each row,
    using the SAME equal-size ``np.linspace`` edges as
    ``train_entry_option_payoff_v1.decile_lift_table`` so the "top decile"
    here is exactly the set of bars that report called the top decile.
    A stable argsort keeps tie-breaking deterministic."""
    scores = np.asarray(scores, dtype=float)
    n = len(scores)
    buckets = np.zeros(n, dtype=int)
    if n == 0:
        return buckets
    order = np.argsort(scores, kind="stable")
    edges = np.linspace(0, n, n_buckets + 1).astype(int)
    for i in range(n_buckets):
        buckets[order[edges[i]:edges[i + 1]]] = i + 1
    return buckets


def decile_spread(
    scores: np.ndarray, payoff: np.ndarray, *, n_buckets: int = DEFAULT_N_BUCKETS,
) -> Tuple[float, float, float]:
    """(top-bucket mean payoff, bottom-bucket mean payoff, top - bottom)."""
    payoff = np.asarray(payoff, dtype=float)
    b = assign_rank_buckets(scores, n_buckets=n_buckets)
    top = payoff[b == n_buckets]
    bot = payoff[b == 1]
    top_m = float(top.mean()) if len(top) else float("nan")
    bot_m = float(bot.mean()) if len(bot) else float("nan")
    return top_m, bot_m, top_m - bot_m


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    """NaN-safe Spearman rho (NaN when undefined, e.g. a constant input)."""
    from scipy.stats import spearmanr

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3 or np.ptp(x[ok]) == 0 or np.ptp(y[ok]) == 0:
        return float("nan")
    rho, _ = spearmanr(x[ok], y[ok])
    return float(rho)


def spearman_with_p(x: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
    from scipy.stats import spearmanr

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3 or np.ptp(x[ok]) == 0 or np.ptp(y[ok]) == 0:
        return float("nan"), float("nan")
    rho, p = spearmanr(x[ok], y[ok])
    return float(rho), float(p)


# ── Day concentration ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class DayConcentration:
    n_fires: int
    n_days: int
    fires_per_day_min: int
    fires_per_day_median: float
    fires_per_day_max: int
    total_payoff: float
    top_k_share: Dict[int, float]
    top_days: List[Dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["top_k_share"] = {str(k): v for k, v in self.top_k_share.items()}
        return d


def top_contributing_days(dates: Sequence[str], payoff: np.ndarray, k: int) -> List[str]:
    """The ``k`` days whose SUMMED payoff (over the given rows) is largest --
    "contribution" in the sense of share of the group's total payoff."""
    s = pd.Series(np.asarray(payoff, dtype=float)).groupby(np.asarray(dates)).sum()
    return s.sort_values(ascending=False, kind="stable").index[:k].tolist()


def day_concentration(
    dates: Sequence[str], payoff: np.ndarray, *, top_k: Sequence[int] = (1, 3, 5), n_list: int = 5,
) -> DayConcentration:
    """Concentration of a set of fires (rows) across days: distinct days,
    fires per day, and the share of the set's TOTAL summed payoff that comes
    from its top-k contributing days. Shares can exceed 1.0 when other days
    sum negative -- that is reported as-is, not clipped, because it is
    exactly the "a few days carry everything" signature this checks for."""
    dates = np.asarray(dates)
    payoff = np.asarray(payoff, dtype=float)
    if len(dates) == 0:
        return DayConcentration(0, 0, 0, 0.0, 0, 0.0, {k: float("nan") for k in top_k}, [])
    counts = pd.Series(dates).value_counts()
    sums = pd.Series(payoff).groupby(dates).sum().sort_values(ascending=False, kind="stable")
    total = float(payoff.sum())
    shares = {int(k): (float(sums.iloc[:k].sum() / total) if total != 0 else float("nan")) for k in top_k}
    top_days = [
        {"trade_date": str(d), "n_fires": int(counts[d]), "sum_payoff": round(float(sums[d]), 4),
         "mean_payoff": round(float(sums[d] / counts[d]), 4)}
        for d in sums.index[:n_list]
    ]
    return DayConcentration(
        n_fires=int(len(dates)),
        n_days=int(len(counts)),
        fires_per_day_min=int(counts.min()),
        fires_per_day_median=float(counts.median()),
        fires_per_day_max=int(counts.max()),
        total_payoff=total,
        top_k_share=shares,
        top_days=top_days,
    )


# ── Cluster bootstrap ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class BootstrapCI:
    point: float
    ci_low: float
    ci_high: float
    n_boot: int
    n_clusters: int
    frac_boot_le_zero: float  # one-sided bootstrap "p" for H0: statistic <= 0

    def to_dict(self) -> Dict[str, float]:
        return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in asdict(self).items()}

    @property
    def excludes_zero(self) -> bool:
        return bool(self.ci_low > 0 or self.ci_high < 0)


def cluster_bootstrap(
    df: pd.DataFrame,
    statistic: Callable[[pd.DataFrame], float],
    *,
    cluster_col: str = "trade_date",
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> BootstrapCI:
    """Block bootstrap by cluster: each replicate draws ``n_clusters``
    clusters WITH replacement and concatenates all their rows. In each
    replicate ``cluster_col`` is RELABELLED to the draw index (0..n-1), so a
    day drawn twice is two distinct clusters to any within-day statistic
    (otherwise a groupby would silently merge the copies into one double-
    size day). Percentile CI; replicates where the statistic is NaN are
    dropped (reported n_boot counts only the finite ones)."""
    point = float(statistic(df))
    codes, uniques = pd.factorize(df[cluster_col], sort=True)
    n_clusters = len(uniques)
    if n_clusters < 2:
        return BootstrapCI(point, float("nan"), float("nan"), 0, n_clusters, float("nan"))
    order = np.argsort(codes, kind="stable")
    sizes = np.bincount(codes, minlength=n_clusters)
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    groups = [order[starts[c]:starts[c] + sizes[c]] for c in range(n_clusters)]
    rng = np.random.default_rng(seed)
    stats: List[float] = []
    base = df.reset_index(drop=True)
    for _ in range(n_boot):
        draw = rng.integers(0, n_clusters, size=n_clusters)
        idx = np.concatenate([groups[c] for c in draw])
        rep = base.iloc[idx].copy()
        rep[cluster_col] = np.repeat(np.arange(n_clusters), sizes[draw])
        v = statistic(rep)
        if np.isfinite(v):
            stats.append(float(v))
    if not stats:
        return BootstrapCI(point, float("nan"), float("nan"), 0, n_clusters, float("nan"))
    arr = np.asarray(stats)
    lo, hi = np.quantile(arr, [alpha / 2, 1 - alpha / 2])
    return BootstrapCI(point, float(lo), float(hi), len(arr), n_clusters, float((arr <= 0).mean()))


def bootstrap_mean(values: np.ndarray, *, n_boot: int = DEFAULT_N_BOOT, alpha: float = DEFAULT_ALPHA, seed: int = 0) -> BootstrapCI:
    """Plain i.i.d. bootstrap of a mean -- used on ONE-VALUE-PER-DAY arrays,
    where it IS the day-cluster bootstrap (each value already is a cluster)."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return BootstrapCI(float(v.mean()) if len(v) else float("nan"), float("nan"), float("nan"), 0, len(v), float("nan"))
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), size=(n_boot, len(v)))].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return BootstrapCI(float(v.mean()), float(lo), float(hi), n_boot, len(v), float((means <= 0).mean()))


# ── Day aggregation / within-day decomposition ─────────────────────────────

def aggregate_by_day(
    df: pd.DataFrame,
    *,
    date_col: str = "trade_date",
    score_col: str = "score",
    payoff_col: str = "payoff_score",
    fire_col: Optional[str] = None,
) -> pd.DataFrame:
    """One row per day: n_bars, mean_score, mean_payoff and, if ``fire_col``
    (boolean) is given, n_fires, fire_mean_payoff (NaN on no-fire days) and
    nonfire_mean_payoff."""
    g = df.groupby(date_col, sort=True)
    out = pd.DataFrame({
        "n_bars": g.size(),
        "mean_score": g[score_col].mean(),
        "mean_payoff": g[payoff_col].mean(),
    })
    if fire_col is not None:
        fires = df[df[fire_col].astype(bool)].groupby(date_col)[payoff_col]
        nonfires = df[~df[fire_col].astype(bool)].groupby(date_col)[payoff_col]
        out["n_fires"] = fires.size().reindex(out.index).fillna(0).astype(int)
        out["fire_mean_payoff"] = fires.mean().reindex(out.index)
        out["nonfire_mean_payoff"] = nonfires.mean().reindex(out.index)
    return out.reset_index()


def within_day_spearman(
    df: pd.DataFrame, *, date_col: str = "trade_date", score_col: str = "score",
    payoff_col: str = "payoff_score", min_bars: int = 20,
) -> pd.Series:
    """Per-day Spearman(score, payoff) over that day's bars only -- the
    "which bar within the day" question, with all between-day variation
    removed by construction. Days with fewer than ``min_bars`` bars or an
    undefined rho are dropped."""
    vals: Dict[str, float] = {}
    for d, sub in df.groupby(date_col, sort=True):
        if len(sub) < min_bars:
            continue
        r = spearman(sub[score_col].to_numpy(), sub[payoff_col].to_numpy())
        if np.isfinite(r):
            vals[d] = r
    return pd.Series(vals, dtype=float)


def within_day_bucket_spread(
    df: pd.DataFrame, *, date_col: str = "trade_date", score_col: str = "score",
    payoff_col: str = "payoff_score", n_buckets: int = DEFAULT_N_BUCKETS, min_bars: int = 20,
) -> pd.Series:
    """Per-day (top-bucket mean payoff - bottom-bucket mean payoff), buckets
    assigned WITHIN that day -- i.e. "if I pick this day's top-10% bars by
    score instead of its bottom-10%, how much better do I do"."""
    vals: Dict[str, float] = {}
    for d, sub in df.groupby(date_col, sort=True):
        if len(sub) < max(min_bars, n_buckets):
            continue
        _, _, spread = decile_spread(sub[score_col].to_numpy(), sub[payoff_col].to_numpy(), n_buckets=n_buckets)
        if np.isfinite(spread):
            vals[d] = spread
    return pd.Series(vals, dtype=float)


def demean_by_day(df: pd.DataFrame, cols: Sequence[str], *, date_col: str = "trade_date") -> pd.DataFrame:
    """Return a copy with each of ``cols`` minus its own day mean."""
    out = df.copy()
    for c in cols:
        out[c] = out[c] - out.groupby(date_col)[c].transform("mean")
    return out


def between_day_variance_share(dates: Sequence[str], values: np.ndarray) -> float:
    """R^2 of a day-fixed-effect model: share of total variance in
    ``values`` explained by which DAY a bar is on (between-day sum of
    squares / total sum of squares)."""
    v = pd.Series(np.asarray(values, dtype=float))
    d = np.asarray(dates)
    total = float(((v - v.mean()) ** 2).sum())
    if total == 0:
        return float("nan")
    day_mean = v.groupby(d).transform("mean")
    between = float(((day_mean - v.mean()) ** 2).sum())
    return between / total


def day_mean_score_rho(df: pd.DataFrame, *, date_col: str = "trade_date", score_col: str = "score", payoff_col: str = "payoff_score") -> float:
    """Bar-level Spearman when every bar's score is REPLACED by its day's
    mean score -- how much of the pooled bar-level rho a pure "which day"
    ranking reproduces. (Uses the whole day's scores, so it is an upper
    bound on what a day-level ranker could get, not a tradeable signal.)"""
    s = df.groupby(date_col)[score_col].transform("mean").to_numpy()
    return spearman(s, df[payoff_col].to_numpy())


# ── Robustness to top days ──────────────────────────────────────────────────

def robustness_to_top_days(
    df: pd.DataFrame,
    *,
    ks: Sequence[int] = (0, 1, 3, 5),
    date_col: str = "trade_date",
    score_col: str = "score",
    payoff_col: str = "payoff_score",
    n_buckets: int = DEFAULT_N_BUCKETS,
) -> List[Dict[str, object]]:
    """Identify the top-k days by summed payoff AMONG THE ORIGINAL TOP-BUCKET
    FIRES, drop those days entirely (all their bars), then RE-RANK the
    remaining bars into fresh buckets and recompute spread and Spearman.
    k=0 is the unmodified baseline."""
    b = assign_rank_buckets(df[score_col].to_numpy(), n_buckets=n_buckets)
    fires = df[b == n_buckets]
    ranking = top_contributing_days(fires[date_col].to_numpy(), fires[payoff_col].to_numpy(), max(ks) if ks else 0)
    rows: List[Dict[str, object]] = []
    for k in ks:
        dropped = ranking[:k]
        sub = df[~df[date_col].isin(dropped)]
        top_m, bot_m, spread = decile_spread(sub[score_col].to_numpy(), sub[payoff_col].to_numpy(), n_buckets=n_buckets)
        rho, p = spearman_with_p(sub[score_col].to_numpy(), sub[payoff_col].to_numpy())
        rows.append({
            "k_dropped": int(k),
            "dropped_days": list(dropped),
            "n_rows": int(len(sub)),
            "n_days": int(sub[date_col].nunique()),
            "top_mean": round(top_m, 5),
            "bottom_mean": round(bot_m, 5),
            "spread": round(spread, 5),
            "spearman_rho": round(rho, 4),
            "spearman_p_bar_level": p,
        })
    return rows


# ── Early-session "call the day in advance" ─────────────────────────────────

def early_session_day_frame(
    df: pd.DataFrame,
    *,
    cutoff: str,
    date_col: str = "trade_date",
    time_col: str = "time",
    score_col: str = "score",
    payoff_col: str = "payoff_score",
    early_mean_cols: Sequence[str] = (),
    at_cutoff_cols: Sequence[str] = (),
) -> pd.DataFrame:
    """One row per day splitting the session at ``cutoff`` (HH:MM:SS,
    inclusive on the early side):

    - early_mean_score / early_max_score: the model's score over bars with
      ``time <= cutoff`` (each bar's features use only data up to that bar,
      so these are knowable at the cutoff);
    - later_mean_payoff: mean realized payoff of bars with ``time > cutoff``
      (all of which are entered strictly after the cutoff -- no overlap with
      the information used by the early score);
    - ``early_mean_<c>`` for each of ``early_mean_cols`` and ``at_cutoff_<c>``
      (value on the LAST bar at or before the cutoff) for each of
      ``at_cutoff_cols`` -- baseline, equally-knowable predictors.

    Days with no early bar or no later bar are dropped."""
    t = df[time_col].astype(str)
    early = df[t <= cutoff]
    later = df[t > cutoff]
    ge = early.groupby(date_col, sort=True)
    out = pd.DataFrame({
        "n_early": ge.size(),
        "early_mean_score": ge[score_col].mean(),
        "early_max_score": ge[score_col].max(),
    })
    for c in early_mean_cols:
        out[f"early_mean_{c}"] = ge[c].mean()
    if at_cutoff_cols:
        last = early.sort_values([date_col, time_col]).groupby(date_col, sort=True).tail(1).set_index(date_col)
        for c in at_cutoff_cols:
            out[f"at_cutoff_{c}"] = last[c]
    gl = later.groupby(date_col, sort=True)
    out["n_later"] = gl.size()
    out["later_mean_payoff"] = gl[payoff_col].mean()
    out["later_mean_score"] = gl[score_col].mean()
    out = out.dropna(subset=["early_mean_score", "later_mean_payoff"])
    return out.reset_index().rename(columns={"index": date_col})


def partial_spearman(x: np.ndarray, y: np.ndarray, controls: np.ndarray) -> float:
    """Spearman partial correlation of x and y controlling for ``controls``
    (n x k or n): rank-transform everything, OLS-residualize the x and y
    ranks on the control ranks (with intercept), correlate the residuals.
    Rows with any non-finite value are dropped."""
    from scipy.stats import rankdata

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(controls, dtype=float)
    if z.ndim == 1:
        z = z[:, None]
    ok = np.isfinite(x) & np.isfinite(y) & np.all(np.isfinite(z), axis=1)
    if ok.sum() < z.shape[1] + 4:
        return float("nan")
    rx, ry = rankdata(x[ok]), rankdata(y[ok])
    rz = np.column_stack([rankdata(z[ok][:, j]) for j in range(z.shape[1])])
    A = np.column_stack([np.ones(ok.sum()), rz])
    ex = rx - A @ np.linalg.lstsq(A, rx, rcond=None)[0]
    ey = ry - A @ np.linalg.lstsq(A, ry, rcond=None)[0]
    if np.std(ex) == 0 or np.std(ey) == 0:
        return float("nan")
    return float(np.corrcoef(ex, ey)[0, 1])


# ── Fixed-threshold first fire per day ──────────────────────────────────────

def first_fire_per_day(
    df: pd.DataFrame,
    *,
    threshold: float,
    date_col: str = "trade_date",
    time_col: str = "time",
    score_col: str = "score",
    payoff_col: str = "payoff_score",
) -> pd.DataFrame:
    """For each day, the FIRST bar (chronologically) whose score reaches a
    threshold fixed in advance -- the one-trade-per-day view. Columns:
    date, time, score, payoff, and ``day_mean_payoff`` (that day's mean over
    ALL bars, i.e. a random-bar-on-the-same-day benchmark). Days that never
    fire are absent."""
    day_mean = df.groupby(date_col)[payoff_col].mean()
    fired = df[df[score_col] >= threshold].sort_values([date_col, time_col])
    first = fired.groupby(date_col, sort=True).head(1)
    out = first[[date_col, time_col, score_col, payoff_col]].copy()
    out["day_mean_payoff"] = out[date_col].map(day_mean)
    return out.reset_index(drop=True)


# ── Side-agnostic (no hindsight direction) economics ───────────────────────

def verify_side_returns(
    payoff: np.ndarray, ce: np.ndarray, pe: np.ndarray, *, tol: float = 1e-6, max_bad_frac: float = 0.001,
) -> float:
    """Check that ``payoff == max(ce, pe)`` row-wise (where all three are
    finite) and return the mismatch fraction; raise ValueError if it exceeds
    ``max_bad_frac``. Guards against joining a per-side file built at a
    DIFFERENT hold horizon (the BankNifty base ``direction_margin`` view is
    not the 15-bar one -- 99.9% of its rows disagree with the 15-bar
    ``payoff_score``; only ``..._direction_margin_h15`` matches)."""
    payoff = np.asarray(payoff, dtype=float)
    ce = np.asarray(ce, dtype=float)
    pe = np.asarray(pe, dtype=float)
    ok = np.isfinite(payoff) & np.isfinite(ce) & np.isfinite(pe)
    if not ok.any():
        raise ValueError("verify_side_returns: no rows with finite payoff/ce/pe")
    bad = np.abs(np.maximum(ce[ok], pe[ok]) - payoff[ok]) > tol
    frac = float(bad.mean())
    if frac > max_bad_frac:
        raise ValueError(
            f"verify_side_returns: {frac:.2%} of rows have payoff != max(ce, pe) -- the per-side "
            f"returns were probably built at a different horizon than payoff_score"
        )
    return frac


def side_agnostic_bucket_table(
    scores: np.ndarray, ce: np.ndarray, pe: np.ndarray, *, n_buckets: int = DEFAULT_N_BUCKETS,
) -> List[Dict[str, float]]:
    """Per score bucket: mean hindsight payoff ``max(ce, pe)`` (what the
    model was trained/evaluated on), mean ``coin_flip`` = (ce + pe) / 2 (the
    expected net return of buying ONE side chosen without direction skill),
    and CE-only / PE-only means. Rows with a non-finite side return are
    excluded."""
    s = np.asarray(scores, dtype=float)
    ce = np.asarray(ce, dtype=float)
    pe = np.asarray(pe, dtype=float)
    b = assign_rank_buckets(s, n_buckets=n_buckets)
    ok = np.isfinite(ce) & np.isfinite(pe)
    rows: List[Dict[str, float]] = []
    for k in range(1, n_buckets + 1):
        m = (b == k) & ok
        if not m.any():
            rows.append({"bucket": k, "n": 0})
            continue
        rows.append({
            "bucket": k, "n": int(m.sum()),
            "hindsight_max": round(float(np.maximum(ce[m], pe[m]).mean()), 5),
            "coin_flip": round(float(((ce[m] + pe[m]) / 2).mean()), 5),
            "ce_only": round(float(ce[m].mean()), 5),
            "pe_only": round(float(pe[m].mean()), 5),
        })
    return rows


__all__ = [
    "BootstrapCI",
    "DayConcentration",
    "aggregate_by_day",
    "assign_rank_buckets",
    "between_day_variance_share",
    "bootstrap_mean",
    "cluster_bootstrap",
    "day_concentration",
    "day_mean_score_rho",
    "decile_spread",
    "demean_by_day",
    "early_session_day_frame",
    "first_fire_per_day",
    "partial_spearman",
    "robustness_to_top_days",
    "side_agnostic_bucket_table",
    "verify_side_returns",
    "spearman",
    "spearman_with_p",
    "top_contributing_days",
    "within_day_bucket_spread",
    "within_day_spearman",
]
