"""Shared entry-quality gate for long option buying."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Optional, Protocol

from ..contracts import Direction, RiskContext, StrategyVote
from .regime import RegimeSignal
from .snapshot_accessor import SnapshotAccessor


class EntryPolicyDecision(NamedTuple):
    """Outcome of evaluating whether a directional vote is tradable as long premium."""

    allowed: bool
    reason: str
    score: float
    checks: dict[str, str]
    adjustments: dict[str, float]

    @classmethod
    def block(cls, reason: str, checks: dict[str, str]) -> "EntryPolicyDecision":
        return cls(False, reason, 0.0, checks, {})

    @classmethod
    def allow(
        cls,
        reason: str,
        *,
        score: float,
        checks: dict[str, str],
        adjustments: Optional[dict[str, float]] = None,
    ) -> "EntryPolicyDecision":
        return cls(True, reason, max(0.0, min(1.0, score)), checks, dict(adjustments or {}))


class EntryPolicy(Protocol):
    def evaluate(
        self,
        snap: SnapshotAccessor,
        vote: StrategyVote,
        regime: RegimeSignal,
        risk: RiskContext,
    ) -> EntryPolicyDecision:
        ...


@dataclass(frozen=True)
class PolicyConfig:
    vol_ratio_block_min: float = 1.00
    vol_ratio_warn_min: float = 1.35
    vol_ratio_strong_min: float = 1.85
    option_vol_ratio_min: float = 1.00
    option_vol_ratio_strong: float = 1.40
    momentum_5m_min: float = 0.0008
    momentum_5m_strong: float = 0.0020
    momentum_15m_confirm: float = 0.0010
    late_entry_minute: int = 150
    hard_cutoff_minute: int = 240
    late_session_min_conf: float = 0.80
    iv_pct_hard_max: float = 85.0
    iv_pct_soft_max: float = 65.0
    premium_to_or_width_max: float = 1.50
    premium_to_or_width_cheap: float = 0.20
    blocked_regimes: tuple[str, ...] = ("EXPIRY", "HIGH_VOL", "AVOID")
    conditional_regimes: tuple[str, ...] = ("SIDEWAYS",)
    allowed_regimes: tuple[str, ...] = ("TRENDING", "PRE_EXPIRY")
    conditional_min_conf: float = 0.82
    min_policy_score: float = 0.58
    enable_post_halt_resume_boost: bool = True
    post_halt_resume_boost_score: float = 0.05

    @classmethod
    def from_payload(cls, payload: object) -> "PolicyConfig":
        if not isinstance(payload, dict):
            return cls()
        defaults = cls()
        kwargs: dict[str, object] = {}
        for name in cls.__dataclass_fields__:
            if name not in payload:
                continue
            default_value = getattr(defaults, name)
            raw = payload[name]
            try:
                if isinstance(default_value, tuple):
                    kwargs[name] = tuple(str(item).strip().upper() for item in raw)
                elif isinstance(default_value, bool):
                    if isinstance(raw, bool):
                        kwargs[name] = raw
                    else:
                        kwargs[name] = str(raw).strip().lower() in {"1", "true", "yes", "on"}
                elif isinstance(default_value, int) and not isinstance(default_value, bool):
                    kwargs[name] = int(raw)
                else:
                    kwargs[name] = type(default_value)(raw)
            except (TypeError, ValueError):
                continue
        return cls(**kwargs)


class LongOptionEntryPolicy:
    """Decides whether a directional signal is eligible for long option buying."""

    def __init__(self, config: Optional[PolicyConfig] = None) -> None:
        self._cfg = config or PolicyConfig()

    @property
    def config(self) -> PolicyConfig:
        return self._cfg

    def evaluate(
        self,
        snap: SnapshotAccessor,
        vote: StrategyVote,
        regime: RegimeSignal,
        risk: RiskContext,
    ) -> EntryPolicyDecision:
        del risk
        checks: dict[str, str] = {}
        score = 1.0

        volume_result, volume_delta = self._check_volume(snap)
        checks["volume"] = volume_result
        if volume_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"volume: {volume_result}", checks)
        score += volume_delta

        option_liquidity_result, option_liquidity_delta = self._check_option_liquidity(snap, vote)
        checks["option_liquidity"] = option_liquidity_result
        if option_liquidity_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"option_liquidity: {option_liquidity_result}", checks)
        score += option_liquidity_delta

        momentum_result, momentum_delta = self._check_momentum(snap, vote)
        checks["momentum"] = momentum_result
        if momentum_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"momentum: {momentum_result}", checks)
        score += momentum_delta

        timing_result, timing_delta = self._check_timing(snap, vote)
        checks["timing"] = timing_result
        if timing_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"timing: {timing_result}", checks)
        score += timing_delta

        premium_result, premium_delta = self._check_premium(snap, vote)
        checks["premium"] = premium_result
        if premium_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"premium: {premium_result}", checks)
        score += premium_delta

        regime_result, regime_delta = self._check_regime(regime, vote)
        checks["regime"] = regime_result
        if regime_result.startswith("BLOCK"):
            return EntryPolicyDecision.block(f"regime: {regime_result}", checks)
        score += regime_delta

        final_score = max(0.30, min(1.0, score))
        if final_score < self._cfg.min_policy_score:
            return EntryPolicyDecision.block(
                f"score: final_score={final_score:.2f}<{self._cfg.min_policy_score:.2f}",
                checks,
            )
        return EntryPolicyDecision.allow(
            f"allowed score={final_score:.2f}",
            score=final_score,
            checks=checks,
        )

    def _check_volume(self, snap: SnapshotAccessor) -> tuple[str, float]:
        vol_ratio = snap.vol_ratio
        if vol_ratio is None:
            return "PASS:no_data", 0.0
        if vol_ratio < self._cfg.vol_ratio_block_min:
            return f"BLOCK:vol_ratio={vol_ratio:.2f}", 0.0
        if vol_ratio < self._cfg.vol_ratio_warn_min:
            return f"WARN:vol_ratio={vol_ratio:.2f}", -0.15
        if vol_ratio >= self._cfg.vol_ratio_strong_min:
            return f"PASS:vol_ratio={vol_ratio:.2f} strong", 0.10
        return f"PASS:vol_ratio={vol_ratio:.2f}", 0.0

    def _check_momentum(self, snap: SnapshotAccessor, vote: StrategyVote) -> tuple[str, float]:
        direction = vote.direction
        if direction not in (Direction.CE, Direction.PE):
            return "BLOCK:unsupported_direction", 0.0
        r5m = snap.fut_return_5m
        r15m = snap.fut_return_15m
        if r5m is None:
            return "PASS:no_r5m", 0.0
        sign = 1.0 if direction == Direction.CE else -1.0
        directional_r5m = sign * r5m
        if directional_r5m <= 0:
            return f"BLOCK:r5m_wrong_dir={r5m:.4f}", 0.0
        if directional_r5m < self._cfg.momentum_5m_min:
            return f"BLOCK:r5m_too_small={r5m:.4f}", 0.0
        delta = 0.10 if directional_r5m >= self._cfg.momentum_5m_strong else 0.0
        if r15m is None:
            return "PASS:no_r15m", delta
        directional_r15m = sign * r15m
        if directional_r15m < self._cfg.momentum_15m_confirm:
            return f"WARN:r15m_not_confirmed={r15m:.4f}", delta - 0.15
        return f"PASS:r5m={r5m:.4f},r15m={r15m:.4f}", delta

    def _check_option_liquidity(self, snap: SnapshotAccessor, vote: StrategyVote) -> tuple[str, float]:
        direction = vote.direction
        if direction == Direction.CE:
            option_vol_ratio = snap.atm_ce_vol_ratio
        elif direction == Direction.PE:
            option_vol_ratio = snap.atm_pe_vol_ratio
        else:
            return "PASS:not_directional", 0.0
        if option_vol_ratio is None:
            return "PASS:no_option_vol_ratio", 0.0
        if option_vol_ratio < self._cfg.option_vol_ratio_min:
            return f"BLOCK:option_vol_ratio={option_vol_ratio:.2f}", 0.0
        if option_vol_ratio >= self._cfg.option_vol_ratio_strong:
            return f"PASS:option_vol_ratio={option_vol_ratio:.2f} strong", 0.08
        return f"PASS:option_vol_ratio={option_vol_ratio:.2f}", 0.0

    def _check_timing(self, snap: SnapshotAccessor, vote: StrategyVote) -> tuple[str, float]:
        minutes = snap.minutes
        if minutes >= self._cfg.hard_cutoff_minute:
            return f"BLOCK:minutes={minutes}", 0.0
        if minutes >= self._cfg.late_entry_minute and vote.confidence < self._cfg.late_session_min_conf:
            return f"BLOCK:late_low_conf={minutes}", 0.0
        if minutes >= self._cfg.late_entry_minute:
            return f"WARN:late_entry={minutes}", -0.10
        return f"PASS:minutes={minutes}", 0.0

    def _check_premium(self, snap: SnapshotAccessor, vote: StrategyVote) -> tuple[str, float]:
        iv_percentile = snap.iv_percentile
        if iv_percentile is not None:
            if iv_percentile > self._cfg.iv_pct_hard_max:
                return f"BLOCK:iv_percentile={iv_percentile:.1f}", 0.0
            if iv_percentile > self._cfg.iv_pct_soft_max:
                result = f"WARN:iv_percentile={iv_percentile:.1f}"
                base_delta = -0.10
            else:
                result = f"PASS:iv_percentile={iv_percentile:.1f}"
                base_delta = 0.0
        else:
            result = "PASS:no_iv_percentile"
            base_delta = 0.0

        premium = vote.proposed_entry_premium
        or_width = snap.or_width
        if premium is None or premium <= 0 or or_width is None or or_width <= 0:
            return result, base_delta
        premium_ratio = premium / or_width
        if premium_ratio > self._cfg.premium_to_or_width_max:
            return f"WARN:premium_ratio={premium_ratio:.2f}", base_delta - 0.15
        if premium_ratio < self._cfg.premium_to_or_width_cheap:
            if result.startswith("WARN"):
                return result, base_delta + 0.05
            return f"PASS:premium_ratio={premium_ratio:.2f} cheap", 0.05
        return result, base_delta

    def _check_regime(self, regime: RegimeSignal, vote: StrategyVote) -> tuple[str, float]:
        name = regime.regime.value.upper()
        if name in self._cfg.blocked_regimes:
            return f"BLOCK:regime={name}", 0.0
        if name in self._cfg.conditional_regimes:
            if vote.confidence < self._cfg.conditional_min_conf:
                return f"BLOCK:conditional_regime={name}", 0.0
            return f"WARN:conditional_regime={name}", -0.10
        if name in self._cfg.allowed_regimes:
            return f"PASS:regime={name}", 0.05 * max(0.0, min(1.0, regime.confidence))
        return f"PASS:regime={name}", 0.0
