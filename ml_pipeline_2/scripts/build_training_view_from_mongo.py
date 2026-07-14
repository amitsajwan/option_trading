"""Training view from quality-gated Mongo snapshots (weekend rebuild, 2026-07-11).

One table for BOTH the label study and training:
  - features: EXACTLY the serving path (strategy_app.ml.bundle_inference.
    build_feature_row on a SnapshotAccessor) — train/serve parity by
    construction, no separate feature code to drift.
  - labels: a MENU of candidate forward outcomes per bar (the label study
    picks; training consumes the chosen one). Computed from the day's own
    futures closes — no lookahead beyond the labeled horizon.
  - provenance: only days with dataset_manifests.passed=true are eligible
    (the ingest quality gate is enforced end-to-end).

Run inside a container with strategy_app + snapshot_app + pymongo + pandas
(seller_app / dashboard images):
  python -m ml_pipeline_2.scripts.build_training_view_from_mongo \
      --instrument BANKNIFTY --from 2024-11-01 --to 2026-05-23 \
      --out /shared_run/training_view
Output: one csv.gz per instrument (+ a manifest json with row counts).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd


HORIZONS_MIN = (5, 10, 15, 30)


def _feature_list() -> list[str]:
    """The union of the live entry bundles' feature lists — what serving reads."""
    import joblib
    feats: list[str] = []
    for p in ("/app/models/dhan_entry_bundle_v3.joblib",
              "/app/models/nifty_entry_bundle_v3.joblib",
              "/app/models/dhan_entry_fast_v1.joblib"):
        try:
            b = joblib.load(p)
            for f in b.get("features") or []:
                if f not in feats:
                    feats.append(f)
        except Exception:
            pass
    if not feats:
        raise SystemExit("no model bundles found to derive the feature list")
    return feats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="BANKNIFTY")
    ap.add_argument("--from", dest="d_from", required=True)
    ap.add_argument("--to", dest="d_to", required=True)
    ap.add_argument("--out", default="/shared_run/training_view")
    args = ap.parse_args()

    from pymongo import MongoClient
    from strategy_app.market.snapshot_accessor import SnapshotAccessor
    from strategy_app.ml.bundle_inference import build_feature_row

    inst = args.instrument.upper()
    coll_name = ("phase1_market_snapshots_hist" if inst == "BANKNIFTY"
                 else f"phase1_market_snapshots_hist_{inst.lower()}")
    db = MongoClient(os.getenv("MONGO_HOST", "mongo"), 27017)[os.getenv("MONGO_DB", "trading_ai")]

    verified_days = {m["trade_date"] for m in db["dataset_manifests"].find(
        {"passed": True, "instrument": inst,
         "trade_date": {"$gte": args.d_from, "$lte": args.d_to}})}
    print(f"{inst}: {len(verified_days)} quality-verified days in range", flush=True)

    features = _feature_list()

    def _enrich_ema_family(day_rows: list[dict]) -> None:
        """Backfilled snapshots lack the live builder's stateful EMA/compression
        accumulators (2026-07-11 gap: 6 features incl. the model's top-importance
        ema_50_slope were 100% NaN). Compute them per day via the CANONICAL
        modules (feature_engine._ema + compression_features.add_compression_
        features) — no reimplementation, no drift. Only fills values that are
        currently NaN/missing."""
        import math

        from snapshot_app.core.compression_features import add_compression_features
        from snapshot_app.core.feature_engine import _ema

        df = pd.DataFrame({
            "close": [r.get("fut_close") for r in day_rows],
            "high": [r.get("_fut_high") for r in day_rows],
            "low": [r.get("_fut_low") for r in day_rows],
            "volume": [r.get("_fut_vol") for r in day_rows],
        })
        close = df["close"].astype(float)
        for span in (9, 21, 50):
            df[f"ema_{span}"] = _ema(close, span)
        df["day_high"] = df["high"].cummax()
        df["day_low"] = df["low"].cummin()
        add_compression_features(df)
        nz = close.replace(0.0, float("nan"))
        df["ema_9_slope"] = df["ema_9"].diff() / nz
        df["ema_21_slope"] = df["ema_21"].diff() / nz
        df["ema_50_slope"] = df["ema_50"].diff() / nz
        for i, r in enumerate(day_rows):
            for col in df.columns:
                if col in ("close", "high", "low", "volume"):
                    continue
                cur = r.get(col)
                if cur is None or (isinstance(cur, float) and math.isnan(cur)):
                    v = df[col].iloc[i]
                    r[col] = None if pd.isna(v) else float(v)

    def _enrich_velocity_family(
        day_rows: list[dict], raw_snaps: list[dict],
        prev_day_close, prev_day_midday_vol, avg_20d_midday_vol,
    ) -> None:
        """Backfilled snapshots also lack the live builder's stateful velocity/
        AM-context accumulators (2026-07-14 gap: 38 features — the entire vel_*
        and ctx_am_*/ctx_gap_* families — were 100% NaN in both instruments'
        training views, found while investigating a NIFTY rare-label rebuild
        that failed at AUC 0.625 with only 9/47 features populated). Computed
        via the CANONICAL snapshot_app.core.velocity_features.
        compute_per_bar_velocity_df — the SAME function LiveVelocityAccumulator
        uses live ("zero skew" per its own docstring). Only fills values that
        are currently NaN/missing (same idempotent pattern as _enrich_ema_family).

        Uses its OWN field extraction (_extract_hist_row below), not live's
        _extract_morning_row: verified against a raw stored doc 2026-07-14 that
        the historical snapshot schema differs from live's in several places —
        futures_bar keys are open/high/low (not fut_open/fut_high/fut_low),
        iv_skew lives under atm_options (not iv_derived), atm_oi_ratio lives
        under chain_aggregates (not atm_options), and total CE/PE OI/volume are
        never pre-aggregated in storage — OI is summed from the strikes[] array
        here; CE/PE VOLUME was never captured per-strike in this collection at
        all, so vel_ce_vol_delta_30m/vel_pe_vol_delta_30m/
        vel_options_vol_acceleration and vwap_fut-dependent ctx_am_vwap_side
        stay unavailable (~3-4 of the 38 target features) — would need a fresh
        Dhan backfill with volume captured, not a re-enrichment of what's here."""
        import math

        def _extract_hist_row(snap: dict) -> dict:
            fb = snap.get("futures_bar") or {}
            ca = snap.get("chain_aggregates") or {}
            ao = snap.get("atm_options") or {}
            strikes = snap.get("strikes") or []
            ce_oi_sum = sum(float(r.get("ce_oi") or 0) for r in strikes) if strikes else None
            pe_oi_sum = sum(float(r.get("pe_oi") or 0) for r in strikes) if strikes else None
            return {
                "timestamp": snap.get("timestamp"),
                "trade_date": snap.get("trade_date"),
                "px_fut_open": fb.get("open"),
                "px_fut_high": fb.get("high"),
                "px_fut_low": fb.get("low"),
                "px_fut_close": fb.get("fut_close"),
                "opt_flow_ce_oi_total": ce_oi_sum,
                "opt_flow_pe_oi_total": pe_oi_sum,
                "opt_flow_pcr_oi": ca.get("pcr"),
                "atm_oi_ratio": ca.get("atm_oi_ratio"),
                "atm_ce_iv": ao.get("atm_ce_iv"),
                "atm_pe_iv": ao.get("atm_pe_iv"),
                "iv_skew": ao.get("iv_skew"),
                # Not present in this collection's schema — left NaN deliberately:
                "opt_flow_ce_volume_total": None,
                "opt_flow_pe_volume_total": None,
                "vwap_fut": None,
            }

        from snapshot_app.core.velocity_features import (
            compute_per_bar_velocity_df, _ALL_OUTPUT_COLUMNS as _VCOLS,
        )

        vdf = pd.DataFrame([_extract_hist_row(s) for s in raw_snaps])
        for col in vdf.columns:
            if col not in ("timestamp", "trade_date"):
                vdf[col] = pd.to_numeric(vdf[col], errors="coerce")
        enriched = compute_per_bar_velocity_df(
            vdf, prev_day_close=prev_day_close,
            prev_day_midday_option_volume=prev_day_midday_vol,
            avg_20d_midday_option_volume=avg_20d_midday_vol,
        )
        for i, r in enumerate(day_rows):
            for col in _VCOLS:
                cur = r.get(col)
                if cur is None or (isinstance(cur, float) and math.isnan(cur)):
                    v = enriched[col].iloc[i] if col in enriched.columns else None
                    r[col] = None if (v is None or pd.isna(v)) else float(v)

    rows: list[dict] = []
    _prev_day_close: float | None = None
    _midday_vol_history: list[float] = []  # most-recent-first, capped at 20
    for day in sorted(verified_days):
        day_rows: list[dict] = []
        raw_snaps: list[dict] = []
        for doc in db[coll_name].find({"trade_date_ist": day}, sort=[("timestamp", 1)]):
            snap = (doc.get("payload") or {}).get("snapshot")
            if not snap:
                continue
            acc = SnapshotAccessor(snap)
            frow = build_feature_row(acc, features) or {}
            fb = snap.get("futures_bar") or {}
            fut = fb.get("fut_close")
            day_rows.append({
                "trade_date": day,
                "time": (snap.get("session_context") or {}).get("time"),
                "fut_close": fut,
                "_fut_high": fb.get("high"), "_fut_low": fb.get("low"),
                "_fut_vol": fb.get("volume"),
                "dte": (snap.get("session_context") or {}).get("days_to_expiry"),
                "iv_pct": (snap.get("iv_derived") or {}).get("iv_percentile"),
                **frow,
            })
            raw_snaps.append(snap)
        _enrich_ema_family(day_rows)
        if raw_snaps:
            prev_day_midday_vol = _midday_vol_history[0] if _midday_vol_history else None
            avg_20d_midday_vol = (
                sum(_midday_vol_history) / len(_midday_vol_history)
                if _midday_vol_history else None
            )
            _enrich_velocity_family(
                day_rows, raw_snaps, _prev_day_close, prev_day_midday_vol, avg_20d_midday_vol,
            )
            _prev_day_close = day_rows[-1].get("fut_close")
            ca = raw_snaps[-1].get("chain_aggregates") or {}
            ce_v, pe_v = ca.get("total_ce_volume"), ca.get("total_pe_volume")
            if ce_v is not None and pe_v is not None:
                _midday_vol_history.insert(0, float(ce_v) + float(pe_v))
                _midday_vol_history = _midday_vol_history[:20]
        # label menu from the day's own close series (no cross-day lookahead)
        closes = [r["fut_close"] for r in day_rows]
        n = len(day_rows)
        for i, r in enumerate(day_rows):
            c0 = closes[i]
            for h in HORIZONS_MIN:
                w = [c for c in closes[i + 1:i + 1 + h] if c]
                if not w or not c0:
                    r[f"fwd_maxabs_{h}m"] = None
                    continue
                r[f"fwd_maxabs_{h}m"] = round(max(abs(c - c0) for c in w), 2)
            r["bars_left"] = n - i - 1
        rows.extend(day_rows)
        if len(rows) % 20000 < 400:
            print(f"  ... {day} total_rows={len(rows)}", flush=True)

    df = pd.DataFrame(rows)
    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, f"training_view_{inst.lower()}.csv.gz")
    df.to_csv(out_path, index=False, compression="gzip")
    manifest = {
        "instrument": inst, "rows": len(df), "days": len(verified_days),
        "features": len(features), "horizons": list(HORIZONS_MIN),
        "from": args.d_from, "to": args.d_to,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_collection": coll_name, "quality_gated": True,
    }
    with open(os.path.join(args.out, f"training_view_{inst.lower()}.manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    print(json.dumps(manifest, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
