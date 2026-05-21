"""Strategy fitness evaluation — maps DayContext to an active strategy set.

StrategyFitnessEvaluator asks each registered StrategyPlugin whether it
fits today's context.  Strategies that declare themselves unfit are
pruned; those that are fit but reduced produce a size_multiplier < 1.

Strategies that do *not* implement StrategyPlugin are always included
at full size — backward compatibility is preserved.

Built-in fitness rules (applied when no StrategyPlugin is registered)
----------------------------------------------------------------------
DayScore.AVOID   → no entries regardless of strategy
DayScore.VOLATILE → reduce size to 0.5×; block short-premium strategies
DayScore.CALM    → short-premium strategies preferred; long-premium reduced
DayScore.NEUTRAL → all strategies at 1.0× (default)
DayScore.UNKNOWN → all strategies at 1.0× (no daily context; defer to intraday)

Adding a new rule
-----------------
1.  Implement StrategyPlugin on the strategy class.
2.  Override ``fits()`` to return a FitnessScore based on the context.
3.  Register the instance with StrategyFitnessEvaluator.register().
The built-in rules in ``_default_fitness`` are the fallback for unregistered
strategies and can be suppressed with ``apply_defaults=False``.
"""

from __future__ import annotations

import logging
from typing import Optional

from .context import DayContext, DayScore, FitnessScore
from .plugin import StrategyPlugin

logger = logging.getLogger(__name__)

# Strategies whose names contain these tokens are "short premium" strategies
# and benefit from CALM but should be reduced/excluded in VOLATILE.
_SHORT_PREMIUM_TOKENS = frozenset({"SHORT", "R1S", "PBV1", "PLAYBOOK"})
# Strategies whose names contain these tokens are "long premium" (debit).
_LONG_PREMIUM_TOKENS = frozenset({"LONG", "DEBIT", "R1_TOP3", "R2_TOP3"})


def _is_short_premium(name: str) -> bool:
    upper = name.upper()
    return any(tok in upper for tok in _SHORT_PREMIUM_TOKENS)


def _is_long_premium(name: str) -> bool:
    upper = name.upper()
    return any(tok in upper for tok in _LONG_PREMIUM_TOKENS)


def _default_fitness(strategy_name: str, context: DayContext) -> FitnessScore:
    """Built-in fitness rules applied to unregistered strategies."""
    score = context.day_score

    if score == DayScore.AVOID:
        return FitnessScore(
            strategy_name=strategy_name,
            fits=False,
            score=0.0,
            reasons=("day_score_avoid",),
            size_multiplier=0.0,
        )

    if score == DayScore.VOLATILE:
        if _is_short_premium(strategy_name):
            return FitnessScore(
                strategy_name=strategy_name,
                fits=False,
                score=0.2,
                reasons=("volatile_regime_blocks_short_premium",),
                size_multiplier=0.0,
            )
        return FitnessScore(
            strategy_name=strategy_name,
            fits=True,
            score=0.6,
            reasons=("volatile_regime_reduced_size",),
            size_multiplier=0.5,
        )

    if score == DayScore.CALM:
        if _is_short_premium(strategy_name):
            return FitnessScore(
                strategy_name=strategy_name,
                fits=True,
                score=0.95,
                reasons=("calm_regime_favours_short_premium",),
                size_multiplier=1.0,
            )
        if _is_long_premium(strategy_name):
            return FitnessScore(
                strategy_name=strategy_name,
                fits=True,
                score=0.55,
                reasons=("calm_regime_reduces_long_premium",),
                size_multiplier=0.6,
            )
        return FitnessScore(
            strategy_name=strategy_name,
            fits=True,
            score=0.80,
            reasons=("calm_regime_normal",),
            size_multiplier=1.0,
        )

    # NEUTRAL or UNKNOWN — full size, no opinion
    return FitnessScore(
        strategy_name=strategy_name,
        fits=True,
        score=0.80,
        reasons=(f"default_full_size:{score.value}",),
        size_multiplier=1.0,
    )


class StrategyFitnessEvaluator:
    """Evaluates all candidate strategies against today's DayContext.

    Usage::

        evaluator = StrategyFitnessEvaluator()
        evaluator.register(MyStrategyPlugin())
        scores = evaluator.evaluate_all(context, ["R1S_TOP3_SHORT_CE", "ORB"])
        active = [s.strategy_name for s in scores if s.fits]
    """

    def __init__(self, *, apply_defaults: bool = True) -> None:
        self._plugins: dict[str, StrategyPlugin] = {}
        self._apply_defaults = apply_defaults

    def register(self, plugin: StrategyPlugin) -> None:
        self._plugins[plugin.name.upper()] = plugin
        logger.debug("fitness plugin registered name=%s", plugin.name)

    def evaluate(self, strategy_name: str, context: DayContext) -> FitnessScore:
        """Score a single strategy against *context*."""
        plugin = self._plugins.get(strategy_name.upper())
        if plugin is not None:
            try:
                return plugin.fits(context)
            except Exception as exc:
                logger.warning(
                    "fitness plugin failed name=%s error=%s", strategy_name, exc
                )
        if self._apply_defaults:
            return _default_fitness(strategy_name, context)
        return FitnessScore(
            strategy_name=strategy_name, fits=True, score=1.0, size_multiplier=1.0
        )

    def evaluate_all(
        self,
        context: DayContext,
        candidate_names: list[str],
    ) -> list[FitnessScore]:
        """Score all candidates; returns list in the same order as input."""
        return [self.evaluate(name, context) for name in candidate_names]

    def active_strategies(
        self,
        context: DayContext,
        candidate_names: list[str],
    ) -> list[str]:
        """Return names of strategies that fit today, in candidate order."""
        return [
            score.strategy_name
            for score in self.evaluate_all(context, candidate_names)
            if score.fits
        ]

    def min_size_multiplier(
        self,
        context: DayContext,
        candidate_names: list[str],
    ) -> float:
        """Return the lowest size_multiplier across all active strategies.

        Used by TradingBrain to compute the session-level lot scaling factor.
        """
        scores = [
            s for s in self.evaluate_all(context, candidate_names) if s.fits
        ]
        if not scores:
            return 0.0
        return min(s.size_multiplier for s in scores)


__all__ = ["StrategyFitnessEvaluator"]
