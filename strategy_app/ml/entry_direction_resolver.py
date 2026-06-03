"""CE/PE side selection for ML entry (no direction-ML).

Combines snapshot momentum/structure with optional live depth (Redis side-channel).
Direction ML is intentionally not used — scores are auditable per source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ..contracts import Direction
from ..market.depth_context import DepthContext, StrikeDepth
from ..market.snapshot_accessor import SnapshotAccessor
from ..runtime.eval_context import get_depth_context


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class EntryDirectionResult:
    direction: Optional[Direction]
    source: str
    ce_score: float = 0.0
    pe_score: float = 0.0
    margin: float = 0.0
    vetoed: bool = False
    veto_reason: str = ""
    sources: dict[str, float] = field(default_factory=dict)

    def as_raw_signals(self) -> dict[str, Any]:
        return {
            "direction_source": self.source,
            "entry_dir_ce_score": round(self.ce_score, 4),
            "entry_dir_pe_score": round(self.pe_score, 4),
            "entry_dir_margin": round(self.margin, 4),
            "entry_dir_vetoed": self.vetoed,
            "entry_dir_veto_reason": self.veto_reason,
            "entry_dir_sources": dict(self.sources),
        }


def _add(ce: float, pe: float, *, w_ce: float = 0.0, w_pe: float = 0.0) -> tuple[float, float]:
    return ce + w_ce, pe + w_pe


def _score_signed(
    ce: float,
    pe: float,
    value: Optional[float],
    weight: float,
    *,
    bullish_is_ce: bool,
) -> tuple[float, float, Optional[str]]:
    if value is None or weight <= 0 or float(value) == 0.0:
        return ce, pe, None
    v = float(value)
    if bullish_is_ce:
        if v > 0:
            ce2, pe2 = _add(ce, pe, w_ce=weight)
            return ce2, pe2, "CE"
        ce2, pe2 = _add(ce, pe, w_pe=weight)
        return ce2, pe2, "PE"
    if v > 0:
        ce2, pe2 = _add(ce, pe, w_pe=weight)
        return ce2, pe2, "PE"
    ce2, pe2 = _add(ce, pe, w_ce=weight)
    return ce2, pe2, "CE"


def _depth_leg_scores(
    leg: StrikeDepth,
    *,
    ce_bullish: bool,
    weight: float,
) -> tuple[float, float, list[str]]:
    """Map one option leg's book to underlying CE vs PE scores."""
    fired: list[str] = []
    ce = 0.0
    pe = 0.0
    if not leg.is_valid or weight <= 0:
        return ce, pe, fired

    bid = float(leg.bid_qty or 0)
    ask = float(leg.ask_qty or 0)
    if ask > 0 and bid > ask * 1.5:
        if ce_bullish:
            ce += weight
            fired.append("bid_dom->CE")
        else:
            pe += weight
            fired.append("bid_dom->PE")
    elif bid > 0 and ask > bid * 2.0:
        if ce_bullish:
            pe += weight
            fired.append("ask_dom->PE")
        else:
            ce += weight
            fired.append("ask_dom->CE")

    imb = leg.qty_imbalance
    if imb is not None and abs(float(imb)) >= 0.15:
        w = weight * min(1.5, abs(float(imb)) / 0.5)
        if ce_bullish:
            if float(imb) > 0:
                ce += w
                fired.append("imb+->CE")
            else:
                pe += w
                fired.append("imb-->PE")
        else:
            if float(imb) > 0:
                pe += w
                fired.append("imb+->PE")
            else:
                ce += w
                fired.append("imb-->CE")

    if leg.microprice is not None and leg.mid is not None and leg.mid > 0:
        skew = (float(leg.microprice) - float(leg.mid)) / float(leg.mid)
        if abs(skew) >= 0.0005:
            w = weight * min(1.2, abs(skew) * 200.0)
            if ce_bullish:
                if skew > 0:
                    ce += w
                    fired.append("micro+->CE")
                else:
                    pe += w
                    fired.append("micro-->PE")
            else:
                if skew > 0:
                    pe += w
                    fired.append("micro+->PE")
                else:
                    ce += w
                    fired.append("micro-->CE")

    rel_spread = leg.relative_spread
    max_spread = _env_float("ENTRY_DIR_MAX_REL_SPREAD", 0.10)
    if rel_spread is not None and float(rel_spread) > max_spread:
        fired.append(f"wide_spread={rel_spread:.3f}")

    return ce, pe, fired


def resolve_entry_direction_composite(
    snap: SnapshotAccessor,
    depth: Optional[DepthContext] = None,
) -> EntryDirectionResult:
    """Score CE vs PE from snapshot + optional depth; veto when margin is too small."""
    min_margin = _env_float("ENTRY_DIR_MIN_MARGIN", 0.35)
    w_r5 = _env_float("ENTRY_DIR_W_MOMENTUM_5M", 1.0)
    w_r15 = _env_float("ENTRY_DIR_W_MOMENTUM_15M", 0.55)
    w_vwap = _env_float("ENTRY_DIR_W_VWAP", 0.65)
    w_vix = _env_float("ENTRY_DIR_W_VIX", 0.75)
    w_iv_skew = _env_float("ENTRY_DIR_W_IV_SKEW", 0.5)
    w_or_trap = _env_float("ENTRY_DIR_W_OR_TRAP", 0.9)
    w_pcr = _env_float("ENTRY_DIR_W_PCR", 0.4)
    w_depth = _env_float("ENTRY_DIR_W_DEPTH", 1.1)

    ce_score = 0.0
    pe_score = 0.0
    sources: dict[str, float] = {}

    ce_score, pe_score, side = _score_signed(ce_score, pe_score, snap.fut_return_5m, w_r5, bullish_is_ce=True)
    if side:
        sources[f"momentum_5m:{side}"] = w_r5

    ce_score, pe_score, side = _score_signed(ce_score, pe_score, snap.fut_return_15m, w_r15, bullish_is_ce=True)
    if side:
        sources[f"momentum_15m:{side}"] = w_r15

    pvwap = snap.price_vs_vwap
    if pvwap is not None and w_vwap > 0 and float(pvwap) != 0.0:
        if float(pvwap) > 0:
            ce_score += w_vwap
            sources["vwap:CE"] = w_vwap
        else:
            pe_score += w_vwap
            sources["vwap:PE"] = w_vwap

    vix_chg = snap.vix_intraday_chg
    if vix_chg is not None and w_vix > 0 and abs(float(vix_chg)) >= 2.0:
        if float(vix_chg) > 0:
            pe_score += w_vix
            sources["vix:PE"] = w_vix
        else:
            ce_score += w_vix
            sources["vix:CE"] = w_vix

    ce_iv = snap.atm_ce_iv
    pe_iv = snap.atm_pe_iv
    if ce_iv and pe_iv and float(ce_iv) > 0 and float(pe_iv) > 0 and w_iv_skew > 0:
        ratio = float(pe_iv) / float(ce_iv)
        if ratio > 1.08:
            pe_score += w_iv_skew
            sources["iv_skew:PE"] = w_iv_skew
        elif ratio < 0.92:
            ce_score += w_iv_skew
            sources["iv_skew:CE"] = w_iv_skew

    if w_or_trap > 0 and snap.or_ready:
        fut = snap.fut_close
        orl = snap.orl
        orh = snap.orh
        if snap.orl_broken and orl is not None and fut is not None and float(fut) > float(orl):
            ce_score += w_or_trap
            sources["orb_low_reject:CE"] = w_or_trap
        if snap.orh_broken and orh is not None and fut is not None and float(fut) < float(orh):
            pe_score += w_or_trap
            sources["orb_high_reject:PE"] = w_or_trap

    pcr_chg = snap.pcr_change_5m
    if pcr_chg is not None and w_pcr > 0 and abs(float(pcr_chg)) >= 0.02:
        # Falling PCR often accompanies call buying / put unwinding (bullish).
        if float(pcr_chg) < 0:
            ce_score += w_pcr
            sources["pcr_5m:CE"] = w_pcr
        else:
            pe_score += w_pcr
            sources["pcr_5m:PE"] = w_pcr

    depth_ctx = depth if depth is not None else get_depth_context()
    if depth_ctx is not None and w_depth > 0:
        # Collect raw per-leg depth contributions first, THEN combine.
        depth_ce = 0.0
        depth_pe = 0.0
        depth_tags: dict[str, float] = {}
        if depth_ctx.ce_valid and depth_ctx.ce is not None:
            d_ce, d_pe, tags = _depth_leg_scores(depth_ctx.ce, ce_bullish=True, weight=w_depth)
            depth_ce += d_ce
            depth_pe += d_pe
            for tag in tags:
                depth_tags[f"depth_ce:{tag}"] = w_depth
        if depth_ctx.pe_valid and depth_ctx.pe is not None:
            d_ce, d_pe, tags = _depth_leg_scores(depth_ctx.pe, ce_bullish=False, weight=w_depth)
            depth_ce += d_ce
            depth_pe += d_pe
            for tag in tags:
                depth_tags[f"depth_pe:{tag}"] = w_depth

        # ── De-correlation ────────────────────────────────────────────────
        # bid_dom / imbalance / microprice off the SAME book are highly
        # correlated: summing them lets a single noisy book manufacture a large
        # directional margin (root cause of the 2026-06-03 fce59da2 CE miss,
        # where 4 depth ticks contributed ~4.4 of a 5.70 CE score). Instead,
        # collapse depth into ONE net signed vote and cap its magnitude so the
        # whole order-book counts at most like a single strong source.
        if _env_int("ENTRY_DIR_DEPTH_DECORRELATE", 1) != 0:
            net = depth_ce - depth_pe
            cap = _env_float("ENTRY_DIR_DEPTH_NET_CAP", w_depth)
            net = max(-cap, min(cap, net))
            if net > 0:
                ce_score += net
            elif net < 0:
                pe_score += -net
            # Keep the raw tags visible for the inspector, but the SCORE impact
            # is the single capped net vote above.
            sources.update(depth_tags)
            if net != 0.0:
                sources["depth_net"] = round(net, 4)
        else:
            ce_score += depth_ce
            pe_score += depth_pe
            sources.update(depth_tags)

    margin = abs(ce_score - pe_score)
    if ce_score <= 0 and pe_score <= 0:
        return EntryDirectionResult(
            direction=None,
            source="composite_veto",
            ce_score=ce_score,
            pe_score=pe_score,
            margin=margin,
            vetoed=True,
            veto_reason="no_direction_signals",
            sources=sources,
        )
    if margin < min_margin:
        return EntryDirectionResult(
            direction=None,
            source="composite_veto",
            ce_score=ce_score,
            pe_score=pe_score,
            margin=margin,
            vetoed=True,
            veto_reason=f"low_margin<{min_margin:.2f}",
            sources=sources,
        )

    direction = Direction.CE if ce_score >= pe_score else Direction.PE
    top = sorted(sources.items(), key=lambda kv: kv[1], reverse=True)[:4]
    basis = ",".join(f"{k}" for k, _ in top) if top else "composite"
    return EntryDirectionResult(
        direction=direction,
        source=f"composite({basis})",
        ce_score=ce_score,
        pe_score=pe_score,
        margin=margin,
        sources=sources,
    )


def resolve_entry_direction_momentum(snap: SnapshotAccessor) -> EntryDirectionResult:
    ret5 = snap.fut_return_5m
    if ret5 is not None and float(ret5) != 0.0:
        direction = Direction.CE if float(ret5) > 0 else Direction.PE
        side = direction.value
        w = 1.0
        ce = w if direction == Direction.CE else 0.0
        pe = w if direction == Direction.PE else 0.0
        return EntryDirectionResult(
            direction=direction,
            source="momentum",
            ce_score=ce,
            pe_score=pe,
            margin=abs(ce - pe),
            sources={"momentum_5m": w},
        )
    return EntryDirectionResult(
        direction=Direction.CE,
        source="momentum_default_ce",
        ce_score=0.5,
        pe_score=0.0,
        margin=0.5,
        sources={"default": 0.5},
    )


def resolve_entry_direction(
    snap: SnapshotAccessor,
    *,
    depth: Optional[DepthContext] = None,
) -> EntryDirectionResult:
    mode = str(os.getenv("ML_ENTRY_DIRECTION_MODE") or "composite").strip().lower()
    if mode in {"momentum", "mom"}:
        return resolve_entry_direction_momentum(snap)
    if mode in {"composite", "multi", "bind", ""}:
        return resolve_entry_direction_composite(snap, depth=depth)
    # Unknown mode — fall back to composite.
    return resolve_entry_direction_composite(snap, depth=depth)
