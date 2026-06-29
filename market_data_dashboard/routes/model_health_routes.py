"""GET /api/model-health — entry model probability distribution + direction stats.

Answers: "is the model degenerate?" (e.g. all probs at 0.82, or max 0.41 vs threshold 0.85).

Data source: strategy_votes (mongo) — has ml_entry_prob per evaluated bar,
including bars that were blocked, not just taken trades.

Lookback: today + last N days (default 5).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Query

try:
    from .._namespace import BASE_VOTES, collection_for
    from ..real_source import make_mongo_db
except ImportError:
    from market_data_dashboard._namespace import BASE_VOTES, collection_for  # type: ignore
    from market_data_dashboard.real_source import make_mongo_db  # type: ignore

logger = logging.getLogger(__name__)
_IST = timezone(timedelta(hours=5, minutes=30))

# Fixed bucket boundaries for the prob histogram.
_BUCKETS = [
    (0.00, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
    (0.20, 0.25), (0.25, 0.30), (0.30, 0.40), (0.40, 0.50),
    (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 1.01),
]


def _bucket_label(lo: float, hi: float) -> str:
    return f"{lo:.2f}-{hi:.2f}"


def _build_histogram(probs: list[float]) -> list[dict[str, Any]]:
    counts = [0] * len(_BUCKETS)
    for p in probs:
        for i, (lo, hi) in enumerate(_BUCKETS):
            if lo <= p < hi:
                counts[i] += 1
                break
    return [
        {"bucket": _bucket_label(lo, hi), "count": counts[i]}
        for i, (lo, hi) in enumerate(_BUCKETS)
    ]


def _date_range(lookback_days: int) -> list[str]:
    today = datetime.now(tz=_IST).date()
    return [
        (today - timedelta(days=i)).isoformat()
        for i in range(lookback_days)
    ]


class ModelHealthRouter:
    """GET /api/model-health — entry model probability distribution."""

    def __init__(self) -> None:
        router = APIRouter(tags=["model-health"])
        router.add_api_route("/api/model-health", self.get_model_health, methods=["GET"])
        self.router = router

    async def get_model_health(
        self,
        instrument: str = Query("BANKNIFTY", description="BANKNIFTY | NIFTY"),
        lookback_days: int = Query(5, ge=1, le=30, description="days to include in histogram"),
        kind: str = Query("live", description="live | oos | sim"),
    ) -> dict[str, Any]:
        dates = _date_range(lookback_days)
        try:
            db = make_mongo_db()
        except Exception as exc:
            return {"error": f"mongo unavailable: {exc}", "instrument": instrument}

        coll = db[collection_for(BASE_VOTES, kind=kind, instrument=instrument)]

        # Pull entry probs from all evaluated bars (ENTRY + SKIP signals).
        docs = list(coll.find(
            {"trade_date_ist": {"$in": dates}},
            {"ml_entry_prob": 1, "ml_ce_prob": 1, "ml_pe_prob": 1,
             "signal_type": 1, "direction": 1, "_id": 0},
        ))

        entry_probs = [
            float(d["ml_entry_prob"])
            for d in docs
            if d.get("ml_entry_prob") is not None
        ]

        threshold = _safe_float(os.getenv("ENTRY_ML_MIN_PROB")) or 0.15

        taken_probs = [p for p in entry_probs if p >= threshold]

        # Direction stats
        ce_probs = [float(d["ml_ce_prob"]) for d in docs if d.get("ml_ce_prob") is not None]
        pe_probs = [float(d["ml_pe_prob"]) for d in docs if d.get("ml_pe_prob") is not None]

        # Model metadata from env / runtime_config
        entry_path = os.getenv("ENTRY_ML_MODEL_PATH", "")
        dir_path = os.getenv("DIRECTION_ML_MODEL_PATH", "")

        result: dict[str, Any] = {
            "instrument": instrument.upper(),
            "lookback_days": lookback_days,
            "dates_included": dates,
            "entry_model": {
                "path": entry_path or None,
                "threshold": threshold,
                "bars_total": len(entry_probs),
                "bars_above_threshold": len(taken_probs),
                "bars_above_threshold_pct": round(100 * len(taken_probs) / max(len(entry_probs), 1), 1),
            },
            "direction_model": {
                "path": dir_path or None,
                "ce_prob_mean": round(sum(ce_probs) / max(len(ce_probs), 1), 4) if ce_probs else None,
                "pe_prob_mean": round(sum(pe_probs) / max(len(pe_probs), 1), 4) if pe_probs else None,
                "n_direction_bars": len(ce_probs),
            },
        }

        if entry_probs:
            result["entry_model"].update({
                "prob_min": round(min(entry_probs), 4),
                "prob_max": round(max(entry_probs), 4),
                "prob_mean": round(sum(entry_probs) / len(entry_probs), 4),
                "prob_p50": round(_percentile(entry_probs, 50), 4),
                "prob_p90": round(_percentile(entry_probs, 90), 4),
                "prob_histogram": _build_histogram(entry_probs),
                # Flag: max output below threshold means zero entries possible
                "degenerate_zero_entries": max(entry_probs) < threshold,
            })
        else:
            result["entry_model"]["prob_histogram"] = []
            result["entry_model"]["degenerate_zero_entries"] = None
            result["entry_model"]["warning"] = "no ml_entry_prob values in strategy_votes for this period"

        return result


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = (pct / 100) * (len(sorted_v) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_v) - 1)
    frac = idx - lo
    return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None
