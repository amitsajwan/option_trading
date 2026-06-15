"""Shared entry direction-resolution policy.

Extracted from ``ml_entry`` so the entry *trigger* (ML probability vs. a
volatility gate vs. anything else) is swappable while the *direction / regime*
logic stays identical and lives in exactly one place. Both ``ML_ENTRY`` and
``VOL_GATE_ENTRY`` call :func:`resolve_direction_for_entry`.

Honors the same env knobs as before: ``ML_ENTRY_PE_ONLY`` / ``ML_ENTRY_CE_ONLY``,
``ML_ENTRY_DIRECTION_MODE`` (composite | consensus | legacy | momentum |
regime_dual), ``DIRECTION_ML_MODEL_PATH``, ``BRAIN_DUAL_MODE``,
``REGIME_ALLOWED``, ``ENTRY_CONFIRM_PREV_TICK``, ``ML_ENTRY_BLOCK_CE/PE``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ...contracts import Direction
from ...market.snapshot_accessor import SnapshotAccessor
from ...ml.bundle_inference import predict_positive_class_prob
from ...ml.entry_direction_resolver import (
    resolve_entry_direction,
    resolve_entry_direction_momentum,
)
from ...utils.env import env_bool

logger = logging.getLogger(__name__)

_DIRECTION_BUNDLE_KIND = "direction_only_bundle"
_DIRECTION_DUAL_BUNDLE_KIND = "direction_dual_bundle"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _load_dir_bundle(path: str) -> Optional[dict[str, Any]]:
    """Load direction bundle — accepts direction_only_bundle or direction_dual_bundle."""
    try:
        import joblib
        bundle = joblib.load(path)
        if not isinstance(bundle, dict):
            return None
        kind = bundle.get("kind", "")
        if kind in (_DIRECTION_BUNDLE_KIND, _DIRECTION_DUAL_BUNDLE_KIND):
            return bundle
        logger.warning("entry_direction: unexpected direction bundle kind=%s at %s", kind, path)
        return None
    except Exception:
        logger.exception("entry_direction: failed to load direction bundle %s", path)
        return None


def _resolve_direction_dual(bundle: dict[str, Any], snap: SnapshotAccessor) -> Optional[Direction]:
    """Pick CE or PE from a dual bundle (argmax by default)."""
    min_prob = _env_float("DIRECTION_DUAL_MIN_PROB", 0.0)
    ce_sub = bundle.get("ce_bundle")
    pe_sub = bundle.get("pe_bundle")
    ce_win = predict_positive_class_prob(ce_sub, snap) if isinstance(ce_sub, dict) else None
    pe_win = predict_positive_class_prob(pe_sub, snap) if isinstance(pe_sub, dict) else None

    if ce_win is None and pe_win is None:
        return None
    if pe_win is None:
        if min_prob > 0 and (ce_win or 0.0) < min_prob:
            return None
        return Direction.CE
    if ce_win is None:
        if min_prob > 0 and (pe_win or 0.0) < min_prob:
            return None
        return Direction.PE
    if ce_win >= pe_win:
        if min_prob > 0 and ce_win < min_prob:
            return None
        return Direction.CE
    if min_prob > 0 and pe_win < min_prob:
        return None
    return Direction.PE


def _apply_direction_block(
    direction: Optional[Direction],
    source: str,
) -> tuple[Optional[Direction], str]:
    if direction is None:
        return None, source
    if env_bool("ML_ENTRY_BLOCK_CE") and direction == Direction.CE:
        return None, f"{source}+block_ce"
    if env_bool("ML_ENTRY_BLOCK_PE") and direction == Direction.PE:
        return None, f"{source}+block_pe"
    return direction, source


def _resolve_direction(snap: SnapshotAccessor) -> tuple[Optional[Direction], str]:
    """CE/PE via direction model / momentum fallback. Returns (direction_or_None, source)."""
    if env_bool("ML_ENTRY_PE_ONLY"):
        return Direction.PE, "pe_only"
    if env_bool("ML_ENTRY_CE_ONLY"):
        return Direction.CE, "ce_only"

    direction: Optional[Direction]
    dir_path = os.getenv("DIRECTION_ML_MODEL_PATH", "").strip()
    if dir_path:
        bundle = _load_dir_bundle(dir_path)
        if bundle is not None:
            if bundle.get("kind") == _DIRECTION_DUAL_BUNDLE_KIND:
                direction = _resolve_direction_dual(bundle, snap)
                return _apply_direction_block(direction, "direction_dual_ml")
            ce_prob = predict_positive_class_prob(bundle, snap)
            if ce_prob is not None:
                direction = Direction.CE if ce_prob >= 0.5 else Direction.PE
                return _apply_direction_block(direction, "direction_ml")
    ret5 = snap.fut_return_5m
    if ret5 is not None and ret5 != 0:
        direction = Direction.CE if float(ret5) > 0 else Direction.PE
    else:
        direction = Direction.CE
    return _apply_direction_block(direction, "momentum")


def _conviction_ensemble_direction(
    snap: SnapshotAccessor, raw_signals: dict[str, Any]
) -> tuple[Optional[Direction], dict[str, Any]]:
    """Conviction-gated expert ensemble (OOS-validated ~56% on 2024 big moves,
    stable across all 4 quarters; ~60% on 2026). Each member votes ONLY when
    confident; act on UNANIMOUS agreement, VETO (abstain) if divided.

    Per-member ENABLE flags + thresholds are config-driven (env), so each member
    can be toggled/tuned from Daily Ops:
      DIR_MEMBER_VWAP_ENABLED (1) / DIR_VWAP_MIN_DIST   (0.0015)
      DIR_MEMBER_ORB_ENABLED  (1)
      DIR_MEMBER_STRD_ENABLED (1) / DIR_STRD_MIN_GAP    (0.005)
      DIR_MEMBER_MOM_ENABLED  (0)  -- non-robust (2024 +, 2026 anti); off by default
      DIR_CONVICTION_RULE     (unanimous | majority)

    CORRECT MISSING/DISABLED SEMANTICS: a member that is disabled OR not confident
    OR whose data is absent is simply EXCLUDED from the panel — it never votes and
    never vetoes. The decision is made among PRESENT members only; if none present,
    abstain (caller emits no vote). Default member set is the OOS-stable trio
    (vwap, orb, strd); momentum off (it was +57% 2024 but anti 2026).
    """
    fd = snap.raw_payload.get("futures_derived") or {}
    orr = snap.raw_payload.get("opening_range") or {}
    ao = snap.raw_payload.get("atm_options") or {}

    def _sg(x):
        try:
            x = float(x)
        except (TypeError, ValueError):
            return 0
        return 1 if x > 0 else (-1 if x < 0 else 0)

    def _enabled(name: str, default: bool) -> bool:
        return env_bool(f"DIR_MEMBER_{name}_ENABLED", default)

    votes: list[int] = []
    detail: dict[str, int] = {}
    skipped: dict[str, str] = {}

    # 1) VWAP acceptance — confident only when decisively away from VWAP
    if _enabled("VWAP", True):
        pv = fd.get("price_vs_vwap")
        thr = float(os.getenv("DIR_VWAP_MIN_DIST", "0.0015") or 0.0015)
        if pv is None:
            skipped["vwap"] = "missing"
        elif abs(float(pv)) <= thr:
            skipped["vwap"] = "not_confident"
        else:
            detail["vwap"] = _sg(pv); votes.append(detail["vwap"])
    else:
        skipped["vwap"] = "disabled"

    # 2) Opening-range break — confident only on a clean break
    if _enabled("ORB", True):
        orb = (1 if orr.get("orh_broken") else 0) - (1 if orr.get("orl_broken") else 0)
        if orb == 0:
            skipped["orb"] = "not_confident"
        else:
            detail["orb"] = orb; votes.append(orb)
    else:
        skipped["orb"] = "disabled"

    # 3) Directional straddle expansion — CE expanding faster than PE => bullish
    if _enabled("STRD", True):
        ce, pe = ao.get("atm_ce_return_1m"), ao.get("atm_pe_return_1m")
        gap = float(os.getenv("DIR_STRD_MIN_GAP", "0.005") or 0.005)
        if ce is None or pe is None:
            skipped["strd"] = "missing"
        elif abs(float(ce) - float(pe)) <= gap:
            skipped["strd"] = "not_confident"
        else:
            detail["strd"] = _sg(float(ce) - float(pe)); votes.append(detail["strd"])
    else:
        skipped["strd"] = "disabled"

    # 4) Momentum agreement (1/3/5m unanimous) — OFF by default (non-robust)
    if _enabled("MOM", False):
        mv = [_sg(fd.get("fut_return_1m")), _sg(fd.get("fut_return_3m")), _sg(fd.get("fut_return_5m"))]
        if 0 not in mv and abs(sum(mv)) == 3:
            detail["mom"] = mv[0]; votes.append(mv[0])
        else:
            skipped["mom"] = "not_confident"
    else:
        skipped["mom"] = "disabled"

    raw_signals["direction_source"] = "conviction_ensemble"
    raw_signals["conviction_votes"] = detail
    raw_signals["conviction_skipped"] = skipped
    if not votes:
        raw_signals["conviction_result"] = "no_present_member"
        return None, raw_signals

    rule = (os.getenv("DIR_CONVICTION_RULE", "unanimous") or "unanimous").strip().lower()
    if rule == "majority":
        s = sum(votes)
        if s == 0:
            raw_signals["conviction_result"] = "majority_tie_veto"
            return None, raw_signals
        raw_signals["conviction_result"] = "majority_up" if s > 0 else "majority_down"
        return (Direction.CE if s > 0 else Direction.PE), raw_signals
    # unanimous (default): act only if all present members agree, else veto
    if all(v > 0 for v in votes):
        raw_signals["conviction_result"] = "unanimous_up"
        return Direction.CE, raw_signals
    if all(v < 0 for v in votes):
        raw_signals["conviction_result"] = "unanimous_down"
        return Direction.PE, raw_signals
    raw_signals["conviction_result"] = "divided_veto"
    return None, raw_signals


def resolve_direction_for_entry(
    snap: SnapshotAccessor,
) -> tuple[Optional[Direction], dict[str, Any]]:
    """Resolve CE/PE + direction raw_signals for an entry candidate.

    Returns ``(direction, raw_signals)``. ``direction is None`` means *abstain*
    (no trade) — the caller must not emit a vote.
    """
    raw_signals: dict[str, Any] = {"_entry_policy_mode": "bypass"}

    if env_bool("ML_ENTRY_PE_ONLY"):
        raw_signals["direction_source"] = "pe_only"
        return Direction.PE, raw_signals
    if env_bool("ML_ENTRY_CE_ONLY"):
        raw_signals["direction_source"] = "ce_only"
        return Direction.CE, raw_signals

    direction_mode = os.getenv("ML_ENTRY_DIRECTION_MODE", "composite").strip().lower()
    direction: Optional[Direction]

    if direction_mode == "consensus":
        hint_dir, hint_source = _resolve_direction(snap)
        ce_prob: Optional[float] = None
        dir_path = os.getenv("DIRECTION_ML_MODEL_PATH", "").strip()
        if dir_path:
            dir_bundle = _load_dir_bundle(dir_path)
            if dir_bundle is not None and dir_bundle.get("kind") == _DIRECTION_BUNDLE_KIND:
                ce_prob = predict_positive_class_prob(dir_bundle, snap)
        raw_signals.update(
            {
                "_ml_entry_timing_only": True,
                "direction_source": "ml_entry_timing",
                "ml_direction_hint": hint_dir.value if hint_dir else None,
                "ml_direction_ce_prob": round(ce_prob, 4) if ce_prob is not None else None,
                "ml_direction_hint_source": hint_source,
            }
        )
        direction = hint_dir or Direction.CE
        if env_bool("ENTRY_CONFIRM_PREV_TICK") and direction in (Direction.CE, Direction.PE):
            _r1 = snap.fut_return_1m
            if _r1 is not None and float(_r1) != 0.0:
                _mom = Direction.CE if float(_r1) > 0 else Direction.PE
                if _mom != direction:
                    return None, raw_signals  # prev_tick_momentum_disagree
    elif direction_mode in {"legacy", "direction_ml", "bind"}:
        direction, direction_source = _resolve_direction(snap)
        if direction is None:
            return None, raw_signals
        raw_signals["direction_source"] = direction_source
    elif direction_mode in {"conviction_ensemble", "conviction", "ensemble"}:
        direction, raw_signals = _conviction_ensemble_direction(snap, raw_signals)
        if direction is None:
            return None, raw_signals
    elif direction_mode in {"momentum", "mom"}:
        dir_result = resolve_entry_direction_momentum(snap)
        if dir_result.vetoed or dir_result.direction is None:
            return None, raw_signals
        direction = dir_result.direction
        raw_signals.update(dir_result.as_raw_signals())
    elif direction_mode == "regime_dual":
        from ...brain.regime_director import RegimeDirector
        from ...brain.session_bias import get_session_bias_store
        from ...ml.dual_entry_confirmer import DualEntryConfirmer

        _store = get_session_bias_store()
        try:
            _store.refresh_async(snap)
        except Exception:
            pass
        _bias = _store.current()
        if _bias is not None:
            raw_signals.update(_bias.as_sense())
            raw_signals["session_plan"] = (_bias.plan or "")[:200]
        verdict = RegimeDirector().decide(snap, session_bias=_bias)
        confirm = (
            DualEntryConfirmer().confirm(verdict.side, snap)
            if verdict.side in ("CE", "PE")
            else None
        )
        quality = str(getattr(verdict, "quality", "") or "").upper()
        raw_signals.update(
            {
                "regime_side": verdict.side,
                "regime_signal": verdict.signal,
                "regime_quality": quality,
                "regime_trend_dir": getattr(verdict, "trend_dir", None),
                "regime_confidence": round(verdict.confidence, 3),
                "regime_breakdown": verdict.breakdown,
                "regime_reason": verdict.reason,
            }
        )
        if confirm is not None:
            raw_signals.update(confirm.as_raw_signals())
        if os.getenv("BRAIN_DUAL_MODE", "shadow").strip().lower() == "shadow":
            dir_result = resolve_entry_direction(snap)
            if dir_result.vetoed or dir_result.direction is None:
                return None, raw_signals
            direction = dir_result.direction
            raw_signals.update(dir_result.as_raw_signals())
            raw_signals["direction_source"] = "regime_dual_shadow"
        else:
            if verdict.side not in ("CE", "PE"):
                return None, raw_signals
            allowed = {x.strip().upper() for x in
                       os.getenv("REGIME_ALLOWED", "MID,TREND").split(",") if x.strip()}
            if allowed and quality and quality not in allowed:
                return None, raw_signals
            use_confirm = bool(
                os.getenv("ENTRY_CE_MODEL_PATH", "").strip()
                or os.getenv("ENTRY_PE_MODEL_PATH", "").strip()
            )
            if use_confirm and (confirm is None or not confirm.fire):
                return None, raw_signals
            direction = Direction.CE if verdict.side == "CE" else Direction.PE
            raw_signals["direction_source"] = f"regime_dual:{verdict.signal}:{quality}"
    else:
        dir_result = resolve_entry_direction(snap)
        if dir_result.vetoed or dir_result.direction is None:
            return None, raw_signals
        direction = dir_result.direction
        raw_signals.update(dir_result.as_raw_signals())

    direction, block_tag = _apply_direction_block(
        direction, str(raw_signals.get("direction_source") or "")
    )
    if direction is None:
        return None, raw_signals
    if block_tag != str(raw_signals.get("direction_source") or ""):
        raw_signals["direction_source"] = block_tag
    return direction, raw_signals
