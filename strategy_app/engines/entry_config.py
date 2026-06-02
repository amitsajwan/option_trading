"""EntryConfig — single resolved-once config for the entry pipeline.

All env-var reads live exclusively in :meth:`EntryConfig.from_env`.
Gate ``apply()`` methods receive a frozen ``EntryConfig`` and never call
``os.getenv`` themselves, eliminating the "knob not wired" failure mode.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..constants import (
    DEFAULT_CAPITAL_ALLOCATED,
    DEFAULT_MAX_CONSECUTIVE_LOSSES,
    DEFAULT_MAX_DAILY_LOSS_PCT,
    DEFAULT_MAX_LOTS_PER_TRADE,
    DEFAULT_MAX_SESSION_TRADES,
    DEFAULT_RISK_PER_TRADE_PCT,
    MIN_ENTRY_CONFIDENCE,
)

logger = logging.getLogger(__name__)


def _f(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"EntryConfig: bad float for {key}={raw!r}") from exc


def _i(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"EntryConfig: bad int for {key}={raw!r}") from exc


def _b(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _s(env: Mapping[str, str], key: str, default: str) -> str:
    raw = (env.get(key) or "").strip()
    return raw if raw else default


def _parse_time_windows(raw: str) -> tuple[tuple[int, int], ...]:
    """Parse 'HH:MM-HH:MM,…' → tuple of (start_mins, end_mins)."""
    windows: list[tuple[int, int]] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece or "-" not in piece:
            continue
        try:
            a, b = piece.split("-", 1)
            sh, sm = a.strip().split(":")
            eh, em = b.strip().split(":")
            start = int(sh) * 60 + int(sm)
            end = int(eh) * 60 + int(em)
            if end > start:
                windows.append((start, end))
        except (ValueError, IndexError):
            continue
    return tuple(windows)


@dataclass(frozen=True)
class EntryConfig:
    """Fully resolved, immutable entry-pipeline configuration.

    Build once per run via :meth:`from_env`, then pass as a frozen object
    into ``EntryContext``.  Call :meth:`assert_consistency` at engine boot
    to fail loudly on bad configurations.
    """

    # --- gate thresholds ---
    min_confidence: float
    bypass_min_confidence: float
    regime_min_confidence: float

    # --- time / regime filter ---
    entry_time_windows: tuple[tuple[int, int], ...]  # empty = no restriction
    regime_allowed_tags: frozenset[str]              # empty = no restriction
    regime_tagger: str                               # e.g. "gap_03pct", ""

    # --- direction ---
    sideways_min_margin: float
    global_min_margin: float
    ml_direction_weight: float
    ml_block_pe: bool
    ml_block_ce: bool

    # --- strike / depth ---
    strike_policy: str                # "atm" | "smart_strike"
    smart_strike_enabled: bool
    max_premium: float                # 0 = no cap
    hard_premium_cap: bool            # enforce max_premium as hard cap (not just OTM tier)
    max_otm_steps: int
    iv_reject_pctile: float           # 0 = disabled

    # --- risk ---
    max_session_trades: int
    max_consecutive_losses: int
    max_lots_per_trade: int
    capital: float
    per_trade_pct: float

    # --- misc ---
    startup_warmup_minutes: float
    startup_warmup_events: int

    # --- extra passthrough (unknown env, kept for forward compat) ---
    extras: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "EntryConfig":
        """Read all knobs from *env* (defaults to ``os.environ``)."""
        if env is None:
            env = os.environ

        min_conf = _f(env, "STRATEGY_MIN_CONFIDENCE", MIN_ENTRY_CONFIDENCE)
        bypass_min = _f(
            env,
            "CONSENSUS_BYPASS_MIN_CONFIDENCE",
            min_conf,
        )
        regime_min = _f(env, "STRATEGY_REGIME_MIN_CONFIDENCE", 0.60)

        time_windows_raw = _s(env, "ENTRY_TIME_WINDOWS", "")
        time_windows = _parse_time_windows(time_windows_raw) if time_windows_raw else ()

        allowed_tags_raw = _s(env, "ENTRY_REGIME_ALLOWED_TAGS", "")
        allowed_tags = frozenset(
            t.strip().lower() for t in allowed_tags_raw.split(",") if t.strip()
        )

        regime_tagger = _s(env, "ENTRY_REGIME_TAGGER", "")

        sideways_margin = _f(env, "DIRECTION_MIN_MARGIN_SIDEWAYS", 2.0)
        global_margin = _f(env, "DIRECTION_CONSENSUS_MIN_MARGIN", 1.25)
        ml_dir_weight = _f(env, "DIRECTION_CONSENSUS_ML_WEIGHT", 0.35)
        ml_block_pe = _b(env, "ML_ENTRY_BLOCK_PE", False)
        ml_block_ce = _b(env, "ML_ENTRY_BLOCK_CE", False)

        strike_policy = _s(env, "STRATEGY_STRIKE_SELECTION_POLICY", "atm").lower()
        smart_strike = _b(env, "STRATEGY_SMART_STRIKE_ENABLED", False)
        max_premium = _f(env, "SMART_STRIKE_MAX_PREMIUM", 0.0)
        hard_cap = _b(env, "SMART_STRIKE_HARD_PREMIUM_CAP", True)
        max_otm = _i(env, "STRATEGY_STRIKE_MAX_OTM_STEPS", 2)
        iv_pctile = _f(env, "SMART_STRIKE_IV_REJECT_PCTILE", 0.0)

        max_trades = _i(env, "RISK_MAX_SESSION_TRADES", DEFAULT_MAX_SESSION_TRADES)
        max_consec = _i(env, "RISK_MAX_CONSECUTIVE_LOSSES", DEFAULT_MAX_CONSECUTIVE_LOSSES)
        max_lots = _i(env, "RISK_MAX_LOTS_PER_TRADE", DEFAULT_MAX_LOTS_PER_TRADE)
        capital = _f(env, "STRATEGY_CAPITAL_ALLOCATED", DEFAULT_CAPITAL_ALLOCATED)
        per_trade = _f(env, "STRATEGY_RISK_PER_TRADE_PCT", DEFAULT_RISK_PER_TRADE_PCT)

        warmup_min = _f(env, "STRATEGY_STARTUP_WARMUP_MINUTES", 0.0)
        warmup_ev = _i(env, "STRATEGY_STARTUP_WARMUP_EVENTS", 0)

        return cls(
            min_confidence=min_conf,
            bypass_min_confidence=bypass_min,
            regime_min_confidence=regime_min,
            entry_time_windows=time_windows,
            regime_allowed_tags=allowed_tags,
            regime_tagger=regime_tagger,
            sideways_min_margin=sideways_margin,
            global_min_margin=global_margin,
            ml_direction_weight=ml_dir_weight,
            ml_block_pe=ml_block_pe,
            ml_block_ce=ml_block_ce,
            strike_policy=strike_policy,
            smart_strike_enabled=smart_strike,
            max_premium=max_premium,
            hard_premium_cap=hard_cap,
            max_otm_steps=max_otm,
            iv_reject_pctile=iv_pctile,
            max_session_trades=max_trades,
            max_consecutive_losses=max_consec,
            max_lots_per_trade=max_lots,
            capital=capital,
            per_trade_pct=per_trade,
            startup_warmup_minutes=max(0.0, warmup_min),
            startup_warmup_events=max(0, warmup_ev),
        )

    def assert_consistency(self) -> None:
        """Log effective values and assert internal invariants.  Call at engine boot."""
        errors: list[str] = []
        if not (0.0 <= self.min_confidence <= 1.0):
            errors.append(f"min_confidence={self.min_confidence} out of [0,1]")
        if not (0.0 <= self.bypass_min_confidence <= 1.0):
            errors.append(f"bypass_min_confidence={self.bypass_min_confidence} out of [0,1]")
        if not (0.0 <= self.regime_min_confidence <= 1.0):
            errors.append(f"regime_min_confidence={self.regime_min_confidence} out of [0,1]")
        if self.max_premium < 0:
            errors.append(f"max_premium={self.max_premium} < 0")
        if self.sideways_min_margin < 0:
            errors.append(f"sideways_min_margin={self.sideways_min_margin} < 0")
        if self.global_min_margin < 0:
            errors.append(f"global_min_margin={self.global_min_margin} < 0")
        if self.max_session_trades < 1:
            errors.append(f"max_session_trades={self.max_session_trades} < 1")
        if errors:
            raise ValueError("EntryConfig inconsistency: " + "; ".join(errors))

        logger.info(
            "entry_config_effective "
            "min_conf=%.2f bypass_min=%.2f regime_min=%.2f "
            "max_premium=%.0f hard_cap=%s strike_policy=%s "
            "time_windows=%s regime_tags=%s max_trades=%d",
            self.min_confidence,
            self.bypass_min_confidence,
            self.regime_min_confidence,
            self.max_premium,
            self.hard_premium_cap,
            self.strike_policy,
            self.entry_time_windows or "disabled",
            sorted(self.regime_allowed_tags) if self.regime_allowed_tags else "disabled",
            self.max_session_trades,
        )

    def time_window_allows(self, minutes_since_midnight: int) -> bool:
        """Return True if no windows configured or *minutes* falls inside one."""
        if not self.entry_time_windows:
            return True
        return any(
            start <= minutes_since_midnight < end
            for start, end in self.entry_time_windows
        )

    def regime_tag_allows(self, tag: Optional[str]) -> bool:
        """Return True if no tag filter configured or *tag* is in allowed set."""
        if not self.regime_allowed_tags:
            return True
        if not tag or tag == "unknown":
            return False
        return tag.lower() in self.regime_allowed_tags
