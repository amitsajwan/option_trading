"""Microstructure v3 feature builder.

Reads:
  - options/                  (per-strike per-minute OHLC + Volume + OI + expiry)
  - snapshots_ml_flat_v2/     (existing support dataset; provides atm_strike per snapshot)

Writes:
  - snapshots_ml_flat_v3/year=YYYY/YYYY-MM-DD.parquet
      All v2 columns PLUS 11 new microstructure features.

The 11 new features (v3.0, no IV dependency):

  Per-strike OI structure
    oi_atm_pe_ce_ratio
    oi_concentration_5strikes
    max_oi_strike_dist_atm
    oi_skew_4strikes
    oi_atm_pe_minus_ce_5m

  Per-strike Volume structure
    vol_atm_pe_ce_ratio
    vol_otm_vs_atm
    vol_weighted_strike_dist

  Per-strike Premium structure
    ce_pe_premium_ratio_atm
    premium_range_atm_5m
    wing_premium_ratio

  Multi-expiry features (next_week_oi_ratio, near_vs_next_premium_ratio,
  oi_change_next_minus_near_5m) — DROPPED from v3.0 because the source
  options/ parquet only contains the nearest expiry per trading day
  (verified across 2024-10-01, 08, 15, 22, 29: exactly 1 expiry each).
  These features need expiry-laddered data that we don't currently
  ingest. Will revisit in v3.1 alongside synthesized IV if needed.

Usage:
  # smoke test on one day (cheap, verifies computation)
  python -m ml_pipeline_2.scripts.feature_builder.build_microstructure_v3 \
      --date 2024-10-31

  # full backfill 2020-08-03 to 2024-10-31
  python -m ml_pipeline_2.scripts.feature_builder.build_microstructure_v3 \
      --start 2020-08-03 --end 2024-10-31

Design notes:
  - Output is the COMPLETE v3 dataset (v2 cols + 14 new). Downstream stage
    views v3 read from this and re-derive their projections.
  - One parquet per trade_date, partitioned by year. Same convention as v2
    so existing ETL/training code can be pointed at v3_root and just work.
  - When the source v2 row has no atm_strike or no matching options data,
    new feature cols are written as NaN. We never silently substitute.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

ROOT = Path("/opt/option_trading/.data/ml_pipeline/parquet_data")
SRC_V2 = ROOT / "snapshots_ml_flat_v2"
SRC_OPTIONS = ROOT / "options"
DEFAULT_OUT = ROOT / "snapshots_ml_flat_v3"

# BANKNIFTY strike step
STRIKE_STEP = 100


# -----------------------------------------------------------------------------
# Per-snapshot feature computation
# -----------------------------------------------------------------------------


def _parse_expiry(expiry_str: str) -> Optional[date]:
    """Parse '01OCT24' -> date(2024, 10, 1)."""
    if not isinstance(expiry_str, str) or len(expiry_str) < 7:
        return None
    try:
        return datetime.strptime(expiry_str, "%d%b%y").date()
    except ValueError:
        return None


def _rank_expiries(expiry_strs: List[str]) -> Dict[str, int]:
    """Map expiry string -> rank by date (0 = nearest)."""
    pairs = [(s, _parse_expiry(s)) for s in expiry_strs if s]
    pairs = [(s, d) for s, d in pairs if d is not None]
    pairs.sort(key=lambda x: x[1])
    return {s: i for i, (s, _) in enumerate(pairs)}


def _compute_features_for_minute(
    chain_now: pd.DataFrame,
    chain_5m_ago: Optional[pd.DataFrame],
    atm_strike: int,
) -> Dict[str, float]:
    """Compute the 14 v3 features for one snapshot.

    chain_now: DataFrame with columns
        [strike, option_type, expiry_str, close, oi, volume]
        — all rows for ONE timestamp.
    chain_5m_ago: same shape but for the snapshot 5 minutes earlier
        (may be None if data unavailable; features needing it return NaN).
    atm_strike: the snapshot's published ATM strike.
    """
    out = {
        "oi_atm_pe_ce_ratio": np.nan,
        "oi_concentration_5strikes": np.nan,
        "max_oi_strike_dist_atm": np.nan,
        "oi_skew_4strikes": np.nan,
        "oi_atm_pe_minus_ce_5m": np.nan,
        "vol_atm_pe_ce_ratio": np.nan,
        "vol_otm_vs_atm": np.nan,
        "vol_weighted_strike_dist": np.nan,
        "ce_pe_premium_ratio_atm": np.nan,
        "premium_range_atm_5m": np.nan,
        "wing_premium_ratio": np.nan,
    }

    if chain_now is None or len(chain_now) == 0 or not atm_strike:
        return out

    # Identify near vs next expiry
    expiry_ranks = _rank_expiries(list(chain_now["expiry_str"].unique()))
    if not expiry_ranks:
        return out
    near_expiry = next((s for s, r in expiry_ranks.items() if r == 0), None)
    next_expiry = next((s for s, r in expiry_ranks.items() if r == 1), None)
    if near_expiry is None:
        return out

    near_chain = chain_now[chain_now["expiry_str"] == near_expiry]
    next_chain = chain_now[chain_now["expiry_str"] == next_expiry] if next_expiry else None

    # Lookups by (strike, option_type)
    def _get(chain: pd.DataFrame, strike: int, otype: str, col: str) -> float:
        if chain is None or len(chain) == 0:
            return np.nan
        m = chain[(chain["strike"] == strike) & (chain["option_type"] == otype)]
        if len(m) == 0:
            return np.nan
        v = m[col].iloc[0]
        return float(v) if pd.notna(v) else np.nan

    # --- Per-strike OI structure ---
    atm_pe_oi = _get(near_chain, atm_strike, "PE", "oi")
    atm_ce_oi = _get(near_chain, atm_strike, "CE", "oi")
    if pd.notna(atm_pe_oi) and pd.notna(atm_ce_oi) and atm_ce_oi > 0:
        out["oi_atm_pe_ce_ratio"] = atm_pe_oi / atm_ce_oi

    near_oi_total = near_chain["oi"].sum()
    strikes_in_band = list(range(atm_strike - 5 * STRIKE_STEP, atm_strike + 6 * STRIKE_STEP, STRIKE_STEP))
    band_oi = near_chain[near_chain["strike"].isin(strikes_in_band)]["oi"].sum()
    if near_oi_total > 0:
        out["oi_concentration_5strikes"] = float(band_oi / near_oi_total)

    oi_by_strike = near_chain.groupby("strike")["oi"].sum()
    if len(oi_by_strike) > 0:
        max_oi_strike = int(oi_by_strike.idxmax())
        out["max_oi_strike_dist_atm"] = (max_oi_strike - atm_strike) / max(atm_strike, 1)

    # OI skew: sum CE OI at +1..+4 OTM strikes minus sum PE OI at -1..-4 OTM strikes
    ce_otm = near_chain[(near_chain["option_type"] == "CE") &
                         (near_chain["strike"].isin([atm_strike + i * STRIKE_STEP for i in range(1, 5)]))]["oi"].sum()
    pe_otm = near_chain[(near_chain["option_type"] == "PE") &
                         (near_chain["strike"].isin([atm_strike - i * STRIKE_STEP for i in range(1, 5)]))]["oi"].sum()
    total_wing = ce_otm + pe_otm
    if total_wing > 0:
        out["oi_skew_4strikes"] = float((ce_otm - pe_otm) / total_wing)

    # 5-minute OI delta at ATM
    if chain_5m_ago is not None and len(chain_5m_ago) > 0 and near_expiry in chain_5m_ago["expiry_str"].values:
        near_5m = chain_5m_ago[chain_5m_ago["expiry_str"] == near_expiry]
        atm_pe_oi_5m = _get(near_5m, atm_strike, "PE", "oi")
        atm_ce_oi_5m = _get(near_5m, atm_strike, "CE", "oi")
        if pd.notna(atm_pe_oi) and pd.notna(atm_pe_oi_5m) and pd.notna(atm_ce_oi) and pd.notna(atm_ce_oi_5m):
            out["oi_atm_pe_minus_ce_5m"] = (atm_pe_oi - atm_pe_oi_5m) - (atm_ce_oi - atm_ce_oi_5m)

    # --- Per-strike Volume structure ---
    atm_pe_vol = _get(near_chain, atm_strike, "PE", "volume")
    atm_ce_vol = _get(near_chain, atm_strike, "CE", "volume")
    if pd.notna(atm_pe_vol) and pd.notna(atm_ce_vol) and atm_ce_vol > 0:
        out["vol_atm_pe_ce_ratio"] = atm_pe_vol / atm_ce_vol

    otm_strikes = [atm_strike + i * STRIKE_STEP for i in (-3, -2, -1, 1, 2, 3) if (atm_strike + i * STRIKE_STEP) != atm_strike]
    atm_zone = [atm_strike - STRIKE_STEP, atm_strike, atm_strike + STRIKE_STEP]
    vol_otm = near_chain[near_chain["strike"].isin(otm_strikes)]["volume"].sum()
    vol_atm = near_chain[near_chain["strike"].isin(atm_zone)]["volume"].sum()
    if vol_atm > 0:
        out["vol_otm_vs_atm"] = float(vol_otm / vol_atm)

    # Volume-weighted strike distance from ATM
    near_with_vol = near_chain[near_chain["volume"] > 0]
    if len(near_with_vol) > 0:
        total_vol = near_with_vol["volume"].sum()
        if total_vol > 0:
            wstrike = float((near_with_vol["volume"] * near_with_vol["strike"]).sum() / total_vol)
            out["vol_weighted_strike_dist"] = (wstrike - atm_strike) / max(atm_strike, 1)

    # --- Per-strike Premium structure ---
    atm_pe_close = _get(near_chain, atm_strike, "PE", "close")
    atm_ce_close = _get(near_chain, atm_strike, "CE", "close")
    if pd.notna(atm_pe_close) and atm_pe_close > 0 and pd.notna(atm_ce_close):
        out["ce_pe_premium_ratio_atm"] = atm_ce_close / atm_pe_close

    # ATM CE intra-5-min range as fraction of close (uses 'high' and 'low')
    atm_ce_high = _get(near_chain, atm_strike, "CE", "high")
    atm_ce_low = _get(near_chain, atm_strike, "CE", "low")
    if pd.notna(atm_ce_high) and pd.notna(atm_ce_low) and pd.notna(atm_ce_close) and atm_ce_close > 0:
        out["premium_range_atm_5m"] = (atm_ce_high - atm_ce_low) / atm_ce_close

    # Wing-vs-body premium ratio
    wing_ce_4 = _get(near_chain, atm_strike + 4 * STRIKE_STEP, "CE", "close")
    wing_pe_4 = _get(near_chain, atm_strike - 4 * STRIKE_STEP, "PE", "close")
    if (pd.notna(atm_ce_close) and pd.notna(atm_pe_close) and
            pd.notna(wing_ce_4) and pd.notna(wing_pe_4)):
        body = atm_ce_close + atm_pe_close
        if body > 0:
            out["wing_premium_ratio"] = float((wing_ce_4 + wing_pe_4) / body)

    # Multi-expiry features are not computable in v3.0 because options/
    # parquet only contains the nearest expiry per trading day. Left here
    # as a placeholder for v3.1 once we ingest expiry-laddered data.

    return out


# -----------------------------------------------------------------------------
# Per-day driver
# -----------------------------------------------------------------------------


def build_one_day(trade_date: date, out_root: Path) -> Tuple[bool, str]:
    """Build v3 features for a single trade_date and write to disk.

    Returns (success, message). Skips silently if v2 or options data missing.
    """
    iso_date = trade_date.isoformat()
    v2_path = SRC_V2 / f"year={trade_date.year}" / f"{iso_date}.parquet"
    if not v2_path.exists():
        return False, f"v2 missing: {v2_path}"

    v2 = pd.read_parquet(v2_path)
    if len(v2) == 0:
        return False, "v2 empty"

    # Options for this month — filter down to this date
    options_path = SRC_OPTIONS / f"year={trade_date.year}" / f"month={trade_date.month:02d}" / "data.parquet"
    if not options_path.exists():
        return False, f"options missing: {options_path}"

    options = pd.read_parquet(options_path, columns=[
        "timestamp", "trade_date", "strike", "option_type", "expiry_str",
        "high", "low", "close", "volume", "oi",
    ])
    # Filter to this trade_date
    if "trade_date" in options.columns:
        options = options[options["trade_date"] == iso_date]
    if len(options) == 0:
        return False, "no options rows for date"

    # Snapshot_id format in v2: "YYYYMMDD_HHMM"
    # Options timestamp is pandas Timestamp.
    # Build a dict: timestamp -> chain DataFrame
    chains_by_ts: Dict[pd.Timestamp, pd.DataFrame] = {ts: g for ts, g in options.groupby("timestamp")}
    sorted_timestamps = sorted(chains_by_ts.keys())

    # Pre-compute a 5-min-ago lookup: for each ts, find chain at ts - 5m
    five_min_lookup: Dict[pd.Timestamp, Optional[pd.Timestamp]] = {}
    ts_set = set(sorted_timestamps)
    for ts in sorted_timestamps:
        target = ts - pd.Timedelta(minutes=5)
        five_min_lookup[ts] = target if target in ts_set else None

    # Iterate v2 rows
    new_rows: List[Dict] = []
    # v2 exposes ATM strike as 'opt_flow_atm_strike' (NOT 'atm_strike'). When
    # that's missing or null we fall back to deriving ATM from px_fut_close
    # at the same step grid used by the canonical chain (STRIKE_STEP).
    for _, row in v2.iterrows():
        snap_id = str(row.get("snapshot_id") or "")
        atm_strike = row.get("opt_flow_atm_strike")
        if pd.isna(atm_strike):
            fut = row.get("px_fut_close")
            if pd.notna(fut) and fut > 0:
                atm_strike = round(float(fut) / STRIKE_STEP) * STRIKE_STEP
        if pd.isna(atm_strike) or atm_strike is None:
            new_rows.append({"snapshot_id": snap_id})
            continue
        try:
            atm_strike = int(atm_strike)
        except (TypeError, ValueError):
            new_rows.append({"snapshot_id": snap_id})
            continue

        # Parse snapshot_id to pandas Timestamp
        if len(snap_id) >= 13 and "_" in snap_id:
            day_part, time_part = snap_id.split("_", 1)
            try:
                ts = pd.Timestamp(f"{day_part[:4]}-{day_part[4:6]}-{day_part[6:8]} {time_part[:2]}:{time_part[2:4]}:00")
            except Exception:
                new_rows.append({"snapshot_id": snap_id})
                continue
        else:
            new_rows.append({"snapshot_id": snap_id})
            continue

        chain_now = chains_by_ts.get(ts)
        if chain_now is None:
            new_rows.append({"snapshot_id": snap_id})
            continue

        five_ts = five_min_lookup.get(ts)
        chain_5m = chains_by_ts.get(five_ts) if five_ts is not None else None

        feats = _compute_features_for_minute(chain_now, chain_5m, atm_strike)
        feats["snapshot_id"] = snap_id
        new_rows.append(feats)

    new_df = pd.DataFrame(new_rows)
    if "snapshot_id" not in new_df.columns:
        return False, "no snapshot_id in computed features"

    # Join new features onto v2 (left join preserves all v2 rows)
    merged = v2.merge(new_df, on="snapshot_id", how="left")

    # Write per-day parquet
    out_dir = out_root / f"year={trade_date.year}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{iso_date}.parquet"
    merged.to_parquet(out_path)

    # Brief stats
    new_cols = [c for c in merged.columns if c not in v2.columns]
    coverage = {c: float(merged[c].notna().mean()) for c in new_cols}
    return True, f"wrote {len(merged)} rows, {len(new_cols)} new cols, coverage={coverage}"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--date", help="Single date (YYYY-MM-DD)")
    p.add_argument("--start", help="Backfill start date inclusive (YYYY-MM-DD)")
    p.add_argument("--end", help="Backfill end date inclusive (YYYY-MM-DD)")
    p.add_argument("--out-root", default=str(DEFAULT_OUT),
                   help=f"Output dataset root (default {DEFAULT_OUT})")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    if args.date and not (args.start or args.end):
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
        ok, msg = build_one_day(d, out_root)
        logger.info("%s -> %s: %s", d, "OK" if ok else "SKIP", msg)
        return 0 if ok else 1

    if not (args.start and args.end):
        p.error("must pass --date OR (--start AND --end)")

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    cur = start
    n_ok, n_skip = 0, 0
    while cur <= end:
        ok, msg = build_one_day(cur, out_root)
        if ok:
            n_ok += 1
            if n_ok % 20 == 0:
                logger.info("%s -> OK (%d/%d days processed so far)", cur, n_ok, (cur - start).days + 1)
        else:
            n_skip += 1
            logger.info("%s -> SKIP: %s", cur, msg)
        cur += timedelta(days=1)
    logger.info("backfill done: %d OK, %d skipped", n_ok, n_skip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
