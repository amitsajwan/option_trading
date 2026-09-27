"""Classic crossover-signal direction study (NIFTY, 2026-09-27).

Fourth of four direction-research tracks run today (see
`ml_pipeline_2.pipeline.crossover_direction`'s module docstring for the full
background: three completed sibling tracks today, all clean negatives, plus
five more historical methodologies -- nine independent tests total, all
converging near a 50-56% coin flip). This track is explicitly requested by
the project owner as a genuinely different angle from everything tried so
far: classic discretionary-trader technical CROSSOVER signals (EMA golden/
death cross, MACD line/signal cross, RSI level crosses), tested as STANDALONE
direction calls -- never blended into a weighted composite score (that
family, `conditional_direction.composite_vote`, is already exhausted) -- plus
a hard AND-gate CONFLUENCE test (2+ independent crossovers agreeing within a
short causal window), which is structurally different from a weighted blend.

This is registry-recorded research only. It must NEVER import
`strategy_app.ml.entry_direction_resolver`,
`strategy_app.engines.strategies.ml_entry`, `strategy_app.brain
.regime_director`, or `strategy_app.market.regime`.

Methodology (matches the three completed sibling tracks' rigor and window
structure EXACTLY, for direct comparability):
  1. Build a FULL trading day's (bar_index 0 onward, 09:15 session start)
     fut_close/ema_9/ema_21/ema_50 series per trade_date straight from the
     raw snapshot archive (`crossover_direction.build_full_day_frame`) --
     NOT from the existing training-view CSV, which truncates each day to
     bar_index 30-350 and would carry a material EMA warm-up bias into every
     day's early rows (see that module's docstring). Compute RSI-14
     (feature_engine-verbatim) and MACD(12,26,9) fresh on the FULL day, then
     every crossover/level-cross/confluence vote, all with proper per-day
     reset and day-boundary masking -- cached to `--full-day-cache-csv` so
     re-running this study doesn't re-read all ~428 days of raw snapshot
     parquet (~1-2 minutes) every time.
  2. Inner-join those vote columns onto the labeled rows of
     `training_view_nifty_direction_condition.csv.gz` (bar_index 30-350, the
     ones with a `payoff_winning_side` ground truth) by (trade_date,
     bar_index) -- keeps the full-day-computed indicator accuracy while
     evaluating only where ground truth exists.
  3. Split into 5 expanding-origin walk-forward windows via
     `conditional_direction.build_expanding_windows`, SAME
     `min_train_days=200` the sibling tracks used on this identical input
     file -- produces the identical window boundaries (test windows
     2025-08-26..2026-07-31).
  4. Per window, per signal (7 configs: 2 EMA crossovers, 1 MACD crossover,
     2 RSI level-cross variants -- trend vs reversal, reported separately --
     and 3 confluence variants), score fire rate + accuracy against
     `payoff_winning_side`, with a bootstrap CI and Monte-Carlo permutation
     p-value against a flat 50% baseline
     (`breakout_agreement_direction.evaluate_signal_accuracy`, reused
     verbatim).
  5. Report every window's numbers individually, plus a pooled (weighted by
     real counts) table across all OK windows.
  6. Decisive test: for every config whose POOLED accuracy clears
     `--stress-test-edge-threshold` (default 0.55), re-score the SAME fired
     bars' outcome using entry premiums sampled 1 and 2 bars AFTER the
     original fire, holding the SAME original target exit bar (mirrors
     `run_repricing_wall_control.py`'s / `study_breakout_agreement_direction
     .py`'s exact design): both (a) whether the FROZEN vote's win/loss
     verdict flips (`conditional_direction.check_direction_repricing_wall`)
     and (b) the mean net (post-cost) return of the SPECIFIC called leg at
     each delay (`validation.check_repricing_wall`).

Usage:
    python -m ml_pipeline_2.scripts.study_crossover_direction \\
        --training-view-csv .run/training_view/training_view_nifty_direction_condition.csv.gz \\
        --parquet-base .data/ml_pipeline/parquet_data \\
        --full-day-cache-csv .run/crossover_direction/nifty_full_day_indicators.csv.gz \\
        --lot-size 65 --horizon-bars 15 \\
        --n-windows 5 --min-train-days 200 \\
        --output-json .run/crossover_direction/nifty_crossover_direction_report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("study_crossover_direction")

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ml_pipeline_2.pipeline.breakout_agreement_direction import (  # noqa: E402
    evaluate_signal_accuracy,
    leg_return_for_side,
)
from ml_pipeline_2.pipeline.conditional_direction import (  # noqa: E402
    WalkForwardWindow,
    build_expanding_windows,
    check_direction_repricing_wall,
)
from ml_pipeline_2.pipeline.crossover_direction import (  # noqa: E402
    DAY_COL,
    adx_di_cross_vote,
    bollinger_breakout_vote,
    build_full_day_frame,
    compute_bollinger_bands,
    compute_di_and_adx,
    compute_macd,
    compute_rsi_14,
    compute_supertrend,
    confluence_vote,
    ema_cross_vote,
    macd_cross_vote,
    pctb_breakout_vote,
    rsi_reversal_vote,
    rsi_trend_cross_vote,
    supertrend_flip_vote,
)
from ml_pipeline_2.pipeline.entry_quality_direction import compute_payoff_at_delay  # noqa: E402
from ml_pipeline_2.pipeline.validation import check_repricing_wall  # noqa: E402
from ml_pipeline_2.scripts.build_option_pnl_training_view import read_day_snapshots  # noqa: E402

DEFAULT_N_WINDOWS = 5
DEFAULT_MIN_TRAIN_DAYS = 200
DEFAULT_EDGE_THRESHOLD = 0.55
DEFAULT_MIN_ROWS = 30

# Display name -> underlying vote column built by build_full_day_indicator_frame.
SIGNAL_COLUMNS: Dict[str, str] = {
    "ema_9_21_cross": "vote_ema_9_21",
    "ema_21_50_cross": "vote_ema_21_50",
    "macd_cross": "vote_macd",
    "rsi_trend_cross_50": "vote_rsi_trend_50",
    "rsi_reversal_30_70": "vote_rsi_reversal",
    "adx_di_cross": "vote_adx_di",
    "adx_di_cross_adx20plus": "vote_adx_di_gated20",
    "bollinger_breakout": "vote_bollinger_breakout",
    "supertrend_flip": "vote_supertrend_flip",
    "confluence_trend_min2_w0": "vote_confluence_trend_min2_w0",
    "confluence_trend_min2_w2": "vote_confluence_trend_min2_w2",
    "confluence_trend_min3_w2": "vote_confluence_trend_min3_w2",
    "confluence_all7_min2_w2": "vote_confluence_all7_min2_w2",
    "confluence_all7_min3_w2": "vote_confluence_all7_min3_w2",
    # Multi-timeframe family: already-live-computed 5-minute indicators from
    # each snapshot's own `mtf_derived` block (market_snapshot.py) -- see
    # crossover_direction.py's module docstring for the MACD-provenance
    # correction this uncovered (MACD DOES exist in this codebase, on 5m,
    # just not in feature_engine.py / on the 1m timeframe).
    "ema_9_21_cross_5m": "vote_ema_9_21_5m",
    "macd_cross_5m": "vote_macd_5m",
    "rsi_trend_cross_50_5m": "vote_rsi_trend_50_5m",
    "rsi_reversal_30_70_5m": "vote_rsi_reversal_5m",
    "bollinger_pctb_breakout_5m": "vote_pctb_breakout_5m",
    "confluence_mtf5m_min2_w2": "vote_confluence_mtf5m_min2_w2",
    "confluence_mtf5m_min2_w5": "vote_confluence_mtf5m_min2_w5",
}


# ── Full-day indicator dataset build (cached) ───────────────────────────────

def build_full_day_indicator_frame(parquet_base: str, trade_dates: Sequence[str]) -> pd.DataFrame:
    """Read every requested trade_date's FULL day of raw snapshots (bar_index
    0 onward), compute fresh RSI-14/MACD and every crossover/confluence vote
    on the concatenated, day-sorted result. See module docstring for why
    this must be a full day, not the truncated training-view CSV."""
    frames = []
    n_found = 0
    for day in trade_dates:
        snaps = read_day_snapshots(parquet_base, day)
        if not snaps:
            log.warning("no snapshots found for trade_date=%s under %s -- skipping", day, parquet_base)
            continue
        n_found += 1
        frames.append(build_full_day_frame(snaps))
    if not frames:
        raise RuntimeError(
            f"no snapshots found for ANY of {len(trade_dates)} requested trade_dates under {parquet_base}"
        )
    log.info("read full-day raw fields for %d/%d requested trade_dates", n_found, len(trade_dates))
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(["trade_date", "bar_index"]).reset_index(drop=True)

    log.info("computing fresh RSI-14 and MACD(12,26,9) (1-minute)...")
    df["rsi_14"] = compute_rsi_14(df)
    macd = compute_macd(df)
    df["macd_line"] = macd["macd_line"]
    df["macd_signal"] = macd["macd_signal"]
    df["macd_hist"] = macd["macd_hist"]

    log.info("computing crossover votes...")
    df["vote_ema_9_21"] = ema_cross_vote(df, fast_col="ema_9", slow_col="ema_21")
    df["vote_ema_21_50"] = ema_cross_vote(df, fast_col="ema_21", slow_col="ema_50")
    df["vote_macd"] = macd_cross_vote(df)
    df["vote_rsi_trend_50"] = rsi_trend_cross_vote(df)
    df["vote_rsi_reversal"] = rsi_reversal_vote(df)

    log.info("computing ADX +DI/-DI, Bollinger bands, Supertrend (if-time-permits family)...")
    di = compute_di_and_adx(df)
    df["plus_di"] = di["plus_di"]
    df["minus_di"] = di["minus_di"]
    df["adx_14_fresh"] = di["adx_14_fresh"]
    df["vote_adx_di"] = adx_di_cross_vote(df)
    df["vote_adx_di_gated20"] = adx_di_cross_vote(df, min_adx=20.0)

    bb = compute_bollinger_bands(df)
    df["bb_upper"] = bb["bb_upper"]
    df["bb_lower"] = bb["bb_lower"]
    df["vote_bollinger_breakout"] = bollinger_breakout_vote(df)

    st = compute_supertrend(df)
    df["supertrend_trend"] = st["supertrend_trend"]
    df["vote_supertrend_flip"] = supertrend_flip_vote(df)

    log.info("computing confluence votes...")
    trend_signals = {
        "ema_9_21": df["vote_ema_9_21"],
        "ema_21_50": df["vote_ema_21_50"],
        "macd": df["vote_macd"],
        "rsi_trend_50": df["vote_rsi_trend_50"],
    }
    df["vote_confluence_trend_min2_w0"] = confluence_vote(trend_signals, day=df[DAY_COL], min_agree=2, window_bars=0)
    df["vote_confluence_trend_min2_w2"] = confluence_vote(trend_signals, day=df[DAY_COL], min_agree=2, window_bars=2)
    df["vote_confluence_trend_min3_w2"] = confluence_vote(trend_signals, day=df[DAY_COL], min_agree=3, window_bars=2)

    all7_signals = dict(trend_signals)
    all7_signals["adx_di"] = df["vote_adx_di"]
    all7_signals["bollinger_breakout"] = df["vote_bollinger_breakout"]
    all7_signals["supertrend_flip"] = df["vote_supertrend_flip"]
    df["vote_confluence_all7_min2_w2"] = confluence_vote(all7_signals, day=df[DAY_COL], min_agree=2, window_bars=2)
    df["vote_confluence_all7_min3_w2"] = confluence_vote(all7_signals, day=df[DAY_COL], min_agree=3, window_bars=2)

    log.info("computing multi-timeframe (5-minute, already-live-computed) votes...")
    df["vote_ema_9_21_5m"] = ema_cross_vote(df, fast_col="mtf_ema_9_5m", slow_col="mtf_ema_21_5m")
    df["vote_macd_5m"] = macd_cross_vote(df, macd_line_col="mtf_macd_line_5m", macd_signal_col="mtf_macd_signal_5m")
    df["vote_rsi_trend_50_5m"] = rsi_trend_cross_vote(df, rsi_col="mtf_rsi_14_5m")
    df["vote_rsi_reversal_5m"] = rsi_reversal_vote(df, rsi_col="mtf_rsi_14_5m")
    df["vote_pctb_breakout_5m"] = pctb_breakout_vote(df, pctb_col="mtf_bb_pct_b_5m")

    mtf5m_signals = {
        "ema_9_21_5m": df["vote_ema_9_21_5m"],
        "macd_5m": df["vote_macd_5m"],
        "rsi_trend_50_5m": df["vote_rsi_trend_50_5m"],
        "pctb_breakout_5m": df["vote_pctb_breakout_5m"],
    }
    df["vote_confluence_mtf5m_min2_w2"] = confluence_vote(mtf5m_signals, day=df[DAY_COL], min_agree=2, window_bars=2)
    # A wider window for the 5m family specifically: each underlying value is
    # only updated once every ~5 one-minute bars (piecewise-constant within
    # its own 5-minute bar), so two genuinely-independent 5m signals can
    # legitimately fire a few bars apart even when reacting to "the same"
    # underlying move -- window_bars=2 (matching the 1m family's window) may
    # be too tight to ever let them agree; window_bars=5 matches the family's
    # own native cadence.
    df["vote_confluence_mtf5m_min2_w5"] = confluence_vote(mtf5m_signals, day=df[DAY_COL], min_agree=2, window_bars=5)
    return df


def load_or_build_full_day_frame(
    parquet_base: str, trade_dates: Sequence[str], cache_csv: Optional[str], *, force_rebuild: bool = False,
) -> pd.DataFrame:
    required_cols = set(SIGNAL_COLUMNS.values())
    if cache_csv and not force_rebuild and Path(cache_csv).exists():
        log.info("loading cached full-day indicator frame from %s", cache_csv)
        df = pd.read_csv(cache_csv)
        df["trade_date"] = df["trade_date"].astype(str)
        cached_dates = set(df["trade_date"].unique())
        missing_dates = sorted(set(trade_dates) - cached_dates)
        missing_cols = sorted(required_cols - set(df.columns))
        if not missing_dates and not missing_cols:
            return df
        if missing_cols:
            log.info("cache is missing column(s) %s -- rebuilding from scratch", missing_cols)
        else:
            log.info("cache is missing %d requested trade_date(s) -- rebuilding from scratch", len(missing_dates))
    df = build_full_day_indicator_frame(parquet_base, trade_dates)
    if cache_csv:
        Path(cache_csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_csv, index=False, compression="gzip" if str(cache_csv).endswith(".gz") else None)
        log.info("wrote full-day indicator cache to %s", cache_csv)
    return df


def build_evaluation_frame(cond_df: pd.DataFrame, full_df: pd.DataFrame) -> pd.DataFrame:
    """Inner-join the FULL-day-computed vote columns onto the labeled
    (bar_index 30-350) rows of the direction_condition training view, keyed
    on (trade_date, bar_index) -- correct full-day warm-up, evaluated only
    where `payoff_winning_side` ground truth exists."""
    vote_cols = sorted(set(SIGNAL_COLUMNS.values()))
    indicator_cols = [
        "rsi_14", "macd_line", "macd_signal", "macd_hist",
        "plus_di", "minus_di", "adx_14_fresh", "bb_upper", "bb_lower", "supertrend_trend",
        "mtf_rsi_14_5m", "mtf_ema_9_5m", "mtf_ema_21_5m",
        "mtf_macd_line_5m", "mtf_macd_signal_5m", "mtf_bb_pct_b_5m",
    ]
    keep_cols = ["trade_date", "bar_index"] + vote_cols + indicator_cols
    merged = cond_df.merge(full_df[keep_cols], on=["trade_date", "bar_index"], how="inner")
    dropped = len(cond_df) - len(merged)
    log.info(
        "merged %d/%d labeled rows to full-day indicator votes (%d dropped -- no matching full-day bar)",
        len(merged), len(cond_df), dropped,
    )
    return merged


# ── Per-window evaluation ────────────────────────────────────────────────────

def select_test_frame(df: pd.DataFrame, window: WalkForwardWindow) -> pd.DataFrame:
    return df[(df["trade_date"] >= window.test_start) & (df["trade_date"] <= window.test_end)].reset_index(drop=True)


def evaluate_window(df: pd.DataFrame, window: WalkForwardWindow, *, min_rows: int = DEFAULT_MIN_ROWS) -> Dict[str, Any]:
    test_df = select_test_frame(df, window)
    result: Dict[str, Any] = {
        "window": window.window_id, "test_start": window.test_start, "test_end": window.test_end,
        "n_test": len(test_df), "signals": {},
    }
    if len(test_df) < min_rows:
        result["status"] = "thin_sample"
        return result
    result["status"] = "ok"
    truth = test_df["payoff_winning_side"]
    for name, col in SIGNAL_COLUMNS.items():
        result["signals"][name] = evaluate_signal_accuracy(test_df[col], truth)
    result["_test_df"] = test_df
    return result


def pool_signal_stats(window_results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Pool each signal's fired-bar correctness across every OK window
    (weighted by real counts, then re-run bootstrap/permutation stats ONCE
    on the FULL pooled vector -- not an average of per-window p-values)."""
    ok_windows = [w for w in window_results if w.get("status") == "ok"]
    pooled: Dict[str, Any] = {}
    for name, col in SIGNAL_COLUMNS.items():
        all_votes = [w["_test_df"][col] for w in ok_windows]
        all_truth = [w["_test_df"]["payoff_winning_side"] for w in ok_windows]
        votes = pd.concat(all_votes, ignore_index=True) if all_votes else pd.Series([], dtype=object)
        truth = pd.concat(all_truth, ignore_index=True) if all_truth else pd.Series([], dtype=object)
        pooled[name] = evaluate_signal_accuracy(votes, truth)
    return pooled


# ── Delayed-action stress test (mirrors study_breakout_agreement_direction.py) ─
# NOTE: this glue is deliberately duplicated (not imported) from that sibling
# study script -- each direction-research script owns its own copy, matching
# entry_quality_direction.winsorize_target's documented precedent for small,
# intentional cross-script duplication over adding a script-to-script import
# dependency between two standalone research entry points.

def recompute_called_leg_at_delays(
    rows: pd.DataFrame,
    votes: Sequence[Optional[str]],
    parquet_base: str,
    *,
    horizon_bars: int,
    lot_size: int,
) -> Dict[int, Dict[str, list]]:
    rows = rows.reset_index(drop=True)
    votes = list(votes)
    n = len(rows)
    out: Dict[int, Dict[str, list]] = {
        d: {"winning_side": [None] * n, "called_return": [None] * n} for d in (0, 1, 2)
    }
    by_date: Dict[str, List[int]] = {}
    for i, d in enumerate(rows["trade_date"].astype(str)):
        by_date.setdefault(d, []).append(i)

    for trade_date, idxs in by_date.items():
        snaps = read_day_snapshots(parquet_base, trade_date)
        if not snaps:
            continue
        for i in idxs:
            bar_index = int(rows.loc[i, "bar_index"])
            side = votes[i]
            for delay in (0, 1, 2):
                bp = compute_payoff_at_delay(
                    snaps, bar_index, horizon_bars=horizon_bars, delay_bars=delay, lot_size=lot_size,
                )
                if bp is None:
                    continue
                out[delay]["winning_side"][i] = bp.winning_side
                out[delay]["called_return"][i] = leg_return_for_side(bp, side)
    return out


def run_delayed_stress_test(
    fired_rows: pd.DataFrame,
    votes: Sequence[Optional[str]],
    parquet_base: str,
    *,
    horizon_bars: int,
    lot_size: int,
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
    min_paired_bars: int = 20,
) -> Dict[str, Any]:
    delays = recompute_called_leg_at_delays(
        fired_rows, votes, parquet_base, horizon_bars=horizon_bars, lot_size=lot_size,
    )
    direction_report = check_direction_repricing_wall(
        votes_at_fire=list(votes), truth_at_fire=delays[0]["winning_side"],
        votes_delay_1=list(votes), truth_delay_1=delays[1]["winning_side"],
        votes_delay_2=list(votes), truth_delay_2=delays[2]["winning_side"],
        min_paired_bars=min_paired_bars, edge_threshold=edge_threshold, raise_on_fail=False,
    )
    payoff_report = check_repricing_wall(
        payoff_at_fire=delays[0]["called_return"], payoff_delay_1=delays[1]["called_return"],
        payoff_delay_2=delays[2]["called_return"], min_paired_bars=min_paired_bars, raise_on_fail=False,
    )
    return {
        "direction_flip_test": asdict(direction_report),
        "called_leg_payoff_test": asdict(payoff_report),
    }


# ── Reporting ────────────────────────────────────────────────────────────────

def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float) and np.isnan(obj):
        return None
    return obj


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-view-csv", default=".run/training_view/training_view_nifty_direction_condition.csv.gz")
    ap.add_argument("--parquet-base", default=".data/ml_pipeline/parquet_data")
    ap.add_argument("--full-day-cache-csv", default=".run/crossover_direction/nifty_full_day_indicators.csv.gz")
    ap.add_argument("--force-rebuild-cache", action="store_true")
    ap.add_argument("--lot-size", type=int, default=65)
    ap.add_argument("--horizon-bars", type=int, default=15)
    ap.add_argument("--n-windows", type=int, default=DEFAULT_N_WINDOWS)
    ap.add_argument("--min-train-days", type=int, default=DEFAULT_MIN_TRAIN_DAYS)
    ap.add_argument("--stress-test-edge-threshold", type=float, default=DEFAULT_EDGE_THRESHOLD)
    ap.add_argument("--skip-stress-test", action="store_true")
    ap.add_argument("--output-json", default=None)
    args = ap.parse_args(argv)

    log.info("loading %s", args.training_view_csv)
    cond_df = pd.read_csv(args.training_view_csv)
    cond_df["trade_date"] = cond_df["trade_date"].astype(str)
    cond_df = cond_df.sort_values(["trade_date", "bar_index"]).reset_index(drop=True)
    log.info(
        "loaded %d rows, %d trade_dates (%s..%s)",
        len(cond_df), cond_df["trade_date"].nunique(), cond_df["trade_date"].min(), cond_df["trade_date"].max(),
    )

    trade_dates = sorted(cond_df["trade_date"].unique().tolist())
    full_df = load_or_build_full_day_frame(
        args.parquet_base, trade_dates, args.full_day_cache_csv, force_rebuild=args.force_rebuild_cache,
    )
    eval_df = build_evaluation_frame(cond_df, full_df)

    windows = build_expanding_windows(
        sorted(eval_df["trade_date"].unique().tolist()), n_windows=args.n_windows, min_train_days=args.min_train_days,
    )
    log.info("built %d walk-forward windows", len(windows))

    window_results = [evaluate_window(eval_df, w) for w in windows]
    pooled = pool_signal_stats(window_results)
    ok_windows = [w for w in window_results if w.get("status") == "ok"]

    stress_results: Dict[str, Any] = {}
    if not args.skip_stress_test and ok_windows:
        for name, col in SIGNAL_COLUMNS.items():
            pooled_stats = pooled.get(name, {})
            acc = pooled_stats.get("accuracy")
            if acc is None or acc < args.stress_test_edge_threshold:
                continue
            fired_frames = []
            fired_votes: List[Optional[str]] = []
            for w in ok_windows:
                v = w["_test_df"][col]
                mask = v.notna()
                fired_frames.append(w["_test_df"].loc[mask, ["trade_date", "bar_index"]])
                fired_votes.extend(v[mask].tolist())
            fired_rows = pd.concat(fired_frames, ignore_index=True) if fired_frames else pd.DataFrame()
            if len(fired_rows) == 0:
                continue
            log.info("running delayed-action stress test for %s (n_fired=%d, pooled accuracy=%.4f)", name, len(fired_rows), acc)
            stress_results[name] = run_delayed_stress_test(
                fired_rows, fired_votes, args.parquet_base,
                horizon_bars=args.horizon_bars, lot_size=args.lot_size,
                edge_threshold=args.stress_test_edge_threshold,
            )

    report = {
        "n_windows": len(windows),
        "signal_columns": SIGNAL_COLUMNS,
        "windows": _json_safe([{k: v for k, v in w.items() if not k.startswith("_")} for w in window_results]),
        "pooled_signal_stats": _json_safe(pooled),
        "stress_test": _json_safe(stress_results),
    }

    print("\n=== Crossover-signal direction study: per-window results ===")
    for w in window_results:
        print(f"\nWindow {w['window']}: test {w['test_start']}..{w['test_end']} (n={w['n_test']})")
        if w.get("status") != "ok":
            print(f"  status={w.get('status')}")
            continue
        for name, stats in w["signals"].items():
            if stats["n_voted"] == 0:
                print(f"  {name:28s} n_voted=0 (no fires this window)")
                continue
            print(
                f"  {name:28s} coverage={stats['coverage']:.4f} n_voted={stats['n_voted']:>6d} "
                f"accuracy={stats['accuracy']} 95%CI=[{stats['bootstrap_ci_low']}, {stats['bootstrap_ci_high']}] "
                f"perm_p={stats['permutation_p_value']}"
            )

    print("\n=== Pooled across all OK windows ===")
    for name, stats in pooled.items():
        if stats["n_voted"] == 0:
            print(f"  {name:28s} n_voted=0")
            continue
        print(
            f"  {name:28s} coverage={stats['coverage']:.4f} n_voted={stats['n_voted']:>6d} "
            f"accuracy={stats['accuracy']} 95%CI=[{stats['bootstrap_ci_low']}, {stats['bootstrap_ci_high']}] "
            f"perm_p={stats['permutation_p_value']}"
        )

    if stress_results:
        print("\n=== Delayed-action stress test (mirrors run_repricing_wall_control.py) ===")
        for name, r in stress_results.items():
            df_r = r["direction_flip_test"]
            pf_r = r["called_leg_payoff_test"]
            print(f"  {name}:")
            print(
                f"    direction: ok={df_r['ok']} n={df_r['n_paired_bars']} "
                f"acc_fire={df_r.get('accuracy_at_fire')} acc_d1={df_r.get('accuracy_delay_1')} "
                f"acc_d2={df_r.get('accuracy_delay_2')} -- {df_r.get('reason')}"
            )
            print(
                f"    payoff:    ok={pf_r['ok']} n={pf_r['n_paired_bars']} "
                f"mean_fire={pf_r.get('mean_payoff_at_fire')} mean_d1={pf_r.get('mean_payoff_delay_1')} "
                f"mean_d2={pf_r.get('mean_payoff_delay_2')} p_d1={pf_r.get('wilcoxon_p_delay_1')} "
                f"p_d2={pf_r.get('wilcoxon_p_delay_2')} -- {pf_r.get('reason')}"
            )
    else:
        print("\n=== Delayed-action stress test ===\n  no config cleared the edge threshold -- nothing to stress-test")

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
