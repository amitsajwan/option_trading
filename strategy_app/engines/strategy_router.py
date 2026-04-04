"""Route snapshots to regime-appropriate strategy sets."""

from __future__ import annotations

import logging
from typing import Optional

from ..contracts import BaseStrategy, PositionContext
from .regime import Regime
from .strategies.all_strategies import (
    EMAcrossoverStrategy,
    HighVolORBStrategy,
    IVRegimeFilter,
    OIBuildupStrategy,
    ORBStrategy,
    PrevDayLevelBreakout,
    VWAPReclaimStrategy,
)

logger = logging.getLogger(__name__)
NON_OWNER_EXIT_CONFIDENCE = 0.80


class StrategyRouter:
    """Maps each regime to the strategies that should run."""

    def __init__(self) -> None:
        self._orb = ORBStrategy()
        self._ema = EMAcrossoverStrategy()
        self._vwap = VWAPReclaimStrategy()
        self._oi = OIBuildupStrategy()
        self._iv_filter = IVRegimeFilter()
        self._high_vol_orb = HighVolORBStrategy()
        self._prev_day = PrevDayLevelBreakout()
        self._strategy_registry: dict[str, BaseStrategy] = {
            self._iv_filter.name: self._iv_filter,
            self._high_vol_orb.name: self._high_vol_orb,
            self._orb.name: self._orb,
            self._ema.name: self._ema,
            self._vwap.name: self._vwap,
            self._oi.name: self._oi,
            self._prev_day.name: self._prev_day,
        }

        self._entry_sets: dict[Regime, list[BaseStrategy]] = {
            Regime.TRENDING: [
                self._iv_filter,
                self._orb,
                self._oi,
                self._prev_day,
            ],
            Regime.SIDEWAYS: [
                self._iv_filter,
                self._vwap,
                self._oi,
            ],
            Regime.EXPIRY: [
                self._iv_filter,
                self._vwap,
            ],
            Regime.PRE_EXPIRY: [
                self._iv_filter,
                self._orb,
                self._oi,
            ],
            Regime.HIGH_VOL: [
                self._iv_filter,
                self._high_vol_orb,
            ],
            Regime.AVOID: [],
        }
        self._exit_strategies: list[BaseStrategy] = [
            self._orb,
            self._vwap,
            self._oi,
        ]
        self._cross_exit_helpers: dict[tuple[str, str], set[str]] = {
            ("PRE_EXPIRY", "OI_BUILDUP"): {"ORB"},
        }
        self._default_profile_id = "det_core_v2"
        self._strategy_profile_id = self._default_profile_id
        self._log_configuration()

    def get_strategies(self, regime: Regime, position: Optional[PositionContext]) -> list[BaseStrategy]:
        if isinstance(position, PositionContext):
            owner_name = str(position.entry_strategy or "").strip().upper()
            owner = self._strategy_registry.get(owner_name)
            helper_names = self._cross_exit_helpers.get(
                (str(position.entry_regime or "").strip().upper(), owner_name),
                set(),
            )
            ordered: list[BaseStrategy] = []
            seen: set[str] = set()
            for strategy in [owner] if owner is not None else []:
                if strategy.name not in seen:
                    ordered.append(strategy)
                    seen.add(strategy.name)
            for helper_name in helper_names:
                helper = self._strategy_registry.get(helper_name)
                if helper is not None and helper.name not in seen:
                    ordered.append(helper)
                    seen.add(helper.name)
            if ordered:
                return ordered
            return self._exit_strategies
        return self._entry_sets.get(regime, [])

    def regime_allows_entry(self, regime: Regime) -> bool:
        return bool(self._entry_sets.get(regime))

    def summary(self) -> dict[str, list[str]]:
        output = {regime.value: [strategy.name for strategy in strategies] for regime, strategies in self._entry_sets.items()}
        output["EXIT_UNIVERSAL"] = [strategy.name for strategy in self._exit_strategies]
        output["STRATEGY_PROFILE"] = [self._strategy_profile_id]
        return output

    @property
    def strategy_profile_id(self) -> str:
        return self._strategy_profile_id

    def exit_vote_priority(
        self,
        *,
        position: PositionContext,
        candidate_strategy: str,
        confidence: float,
    ) -> int:
        strategy_name = str(candidate_strategy or "").strip().upper()
        owner = str(position.entry_strategy or "").strip().upper()
        regime = str(position.entry_regime or "").strip().upper()
        if not strategy_name:
            return 0
        if strategy_name == owner:
            return 3
        helpers = self._cross_exit_helpers.get((regime, owner), set())
        if strategy_name in helpers:
            return 2
        if float(confidence) >= NON_OWNER_EXIT_CONFIDENCE:
            return 1
        return 0

    def all_unique_strategies(self) -> list[BaseStrategy]:
        seen: set[int] = set()
        ordered: list[BaseStrategy] = []
        for strategies in list(self._entry_sets.values()) + [self._exit_strategies]:
            for strategy in strategies:
                strategy_id = id(strategy)
                if strategy_id in seen:
                    continue
                seen.add(strategy_id)
                ordered.append(strategy)
        return ordered

    def add_strategy_to_regime(self, regime: Regime, strategy: BaseStrategy, position: int = -1) -> None:
        target = self._entry_sets.setdefault(regime, [])
        if position < 0:
            target.append(strategy)
        else:
            target.insert(position, strategy)
        logger.info("strategy added regime=%s strategy=%s position=%d", regime.value, strategy.name, position)

    def add_exit_strategy(self, strategy: BaseStrategy) -> None:
        self._exit_strategies.append(strategy)
        logger.info("exit strategy added strategy=%s", strategy.name)

    def replace_strategy_set(self, regime: Regime, strategies: list[BaseStrategy]) -> None:
        self._entry_sets[regime] = list(strategies)
        logger.info("strategy set replaced regime=%s strategies=%s", regime.value, [item.name for item in strategies])

    def _log_configuration(self) -> None:
        logger.info("strategy router configured profile=%s", self._strategy_profile_id)
        for regime, strategies in self._entry_sets.items():
            logger.info("  %s -> %s", regime.value, [strategy.name for strategy in strategies] or [])
        logger.info("  EXIT -> %s", [strategy.name for strategy in self._exit_strategies])

    def available_strategy_names(self) -> list[str]:
        return sorted(self._strategy_registry.keys())

    def configure(self, payload: Optional[dict[str, object]]) -> None:
        """Apply router overrides from run metadata."""
        if not isinstance(payload, dict):
            return
        profile_id = str(payload.get("strategy_profile_id") or "").strip()
        if profile_id:
            self._strategy_profile_id = profile_id
        default_entry = {
            Regime.TRENDING: [self._iv_filter.name, self._orb.name, self._oi.name, self._prev_day.name],
            Regime.SIDEWAYS: [self._iv_filter.name, self._vwap.name, self._oi.name],
            Regime.EXPIRY: [self._iv_filter.name, self._vwap.name],
            Regime.PRE_EXPIRY: [self._iv_filter.name, self._orb.name, self._oi.name],
            Regime.HIGH_VOL: [self._iv_filter.name, self._high_vol_orb.name],
            Regime.AVOID: [],
        }
        default_exit = [self._orb.name, self._vwap.name, self._oi.name]
        iv_filter_payload = payload.get("iv_filter_config")
        if isinstance(iv_filter_payload, dict):
            self._iv_filter.configure(iv_filter_payload)

        enabled_entry_raw = payload.get("enabled_entry_strategies")
        enabled_entry: Optional[set[str]] = None
        if isinstance(enabled_entry_raw, list):
            enabled_entry = {
                str(name).strip().upper()
                for name in enabled_entry_raw
                if str(name).strip().upper() in self._strategy_registry
            }

        regime_map_raw = payload.get("regime_entry_map")
        regime_entry_map: dict[Regime, list[str]] = {}
        if isinstance(regime_map_raw, dict):
            for regime_key, names in regime_map_raw.items():
                try:
                    regime = Regime(str(regime_key).strip().upper())
                except Exception:
                    continue
                if not isinstance(names, list):
                    continue
                valid_names = [
                    str(name).strip().upper()
                    for name in names
                    if str(name).strip().upper() in self._strategy_registry
                ]
                regime_entry_map[regime] = valid_names

        exit_raw = payload.get("exit_strategies")
        configured_exit = []
        if isinstance(exit_raw, list):
            configured_exit = [
                str(name).strip().upper()
                for name in exit_raw
                if str(name).strip().upper() in self._strategy_registry
            ]

        new_entry_sets: dict[Regime, list[BaseStrategy]] = {}
        for regime in (Regime.TRENDING, Regime.SIDEWAYS, Regime.EXPIRY, Regime.PRE_EXPIRY, Regime.HIGH_VOL, Regime.AVOID):
            names = regime_entry_map.get(regime, list(default_entry.get(regime, [])))
            if enabled_entry is not None:
                names = [name for name in names if name in enabled_entry]
            new_entry_sets[regime] = [self._strategy_registry[name] for name in names if name in self._strategy_registry]
        self._entry_sets = new_entry_sets

        if configured_exit:
            self._exit_strategies = [self._strategy_registry[name] for name in configured_exit]
        else:
            self._exit_strategies = [self._strategy_registry[name] for name in default_exit]
        self._log_configuration()
