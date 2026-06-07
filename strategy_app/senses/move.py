"""Move sense — "is a big move loading / releasing?" (board B-1.1).

The validated Phase-0 core (B-0.2 GO): ``loaded = compression AND oi_build`` saw a
>=100pt 10-min move ~49% of the time vs ~32-34% base on the 7 accrued live days.
The sum-of-4 score is RETIRED (non-monotonic); the *pair* is the signal.

The component math mirrors ``ops/research/bigmove_score_backtest.py`` (via the
shared thresholds in ``context.py``). ``expected_move_pt`` / ``prob_100`` /
``prob_200`` are CALIBRATION constants from the B-0.2 sample, attached by state —
refresh them when Phase 0 is re-run on more days.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import SenseVerdict
from .context import COMPRESS_RATIO, OI_BUILD, VELOCITY_K, VOL_SPIKE, compression_ratio

# Calibration from the 7-day Phase-0 sample (B-0.2). {expected_move_pt, prob_100, prob_200}
_CAL_LOADED = {"expected_move_pt": 117.0, "prob_100": 0.49, "prob_200": 0.11}
_CAL_BASE = {"expected_move_pt": 93.0, "prob_100": 0.34, "prob_200": 0.05}


def _opt_flag(context: Mapping[str, Any], key: str, fallback: bool) -> bool:
    v = context.get(key)
    return bool(v) if v is not None else fallback


def _derive_velocity(context: Mapping[str, Any]) -> bool:
    atr_build = context.get("atr_build")
    if not atr_build:
        return False
    ret = abs(float(context.get("last_bar_return") or 0.0))
    return ret > VELOCITY_K * float(atr_build)


def _derive_volume(context: Mapping[str, Any]) -> bool:
    vol_build = float(context.get("vol_build_avg") or 0.0)
    ovol = float(context.get("option_volume") or 0.0)
    return bool(vol_build and ovol > VOL_SPIKE * vol_build)


class MoveSense:
    name = "move"

    def evaluate(self, context: Mapping[str, Any]) -> SenseVerdict:
        # Source-agnostic: prefer the semantic `compression_ratio` (= short-vol / baseline-vol,
        # which the live snapshot exposes as `vol_ratio`); fall back to the backtest's
        # atr_build/atr_base. Either way `ratio < COMPRESS_RATIO` == a coiled spring.
        ratio = compression_ratio(context)
        if ratio is None:
            return SenseVerdict.abstain(self.name, reason="no compression input")
        compression = ratio < COMPRESS_RATIO

        # oi_build: prefer signed `oi_change` (snapshot `fut_oi_change_30m`); else the OI window.
        oi_change = context.get("oi_change")
        if oi_change is not None:
            oi_build = float(oi_change) > 0.0
        else:
            oi_now = float(context.get("oi_now") or 0.0)
            oi_15ago = float(context.get("oi_15ago") or 0.0)
            oi_build = bool(oi_now and oi_15ago and oi_now > oi_15ago * OI_BUILD)

        # velocity/volume are the OPTIONAL release-timing signals (B-0.2: loaded-alone is fine).
        # Prefer explicit flags; else derive from the backtest's atr/volume windows; else False.
        velocity = _opt_flag(context, "velocity_flag", _derive_velocity(context))
        volume = _opt_flag(context, "volume_flag", _derive_volume(context))

        loaded = compression and oi_build
        released = loaded and (velocity or volume)   # re-spec'd OR trigger (D3 refinement)
        score = sum((compression, oi_build, velocity, volume))
        cal = _CAL_LOADED if loaded else _CAL_BASE

        if released:
            verdict, conf = "released", 0.7 if (velocity and volume) else 0.55
        elif loaded:
            verdict, conf = "loaded", 0.5
        else:
            verdict, conf = "quiet", 0.0

        return SenseVerdict(
            sense=self.name,
            verdict=verdict,
            confidence=conf,
            value=float(score),
            evidence={
                "score": score,
                "compression": compression, "oi_build": oi_build,
                "velocity": velocity, "volume": volume,
                "released": released,
                "last_bar_return": float(context.get("last_bar_return") or 0.0),
                "expected_move_pt": cal["expected_move_pt"],
                "prob_100": cal["prob_100"],
                "prob_200": cal["prob_200"],
                "horizon_min": 10,
            },
        )


__all__ = ["MoveSense"]
