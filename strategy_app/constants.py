"""Shared constants for strategy_app to eliminate magic-number duplication."""

from __future__ import annotations

# Market
BANKNIFTY_LOT_SIZE = 15

# Session timing (minutes since midnight)
SOFT_CLOSE_MINUTE = 15 * 60          # 15:00 IST
HARD_CLOSE_MINUTE = 15 * 60 + 15   # 15:15 IST

# Numeric safety
PRICE_EPS = 1e-9

# Engine thresholds
MIN_ENTRY_CONFIDENCE = 0.65
EXIT_CONFIDENCE = 0.65

# Capital / risk defaults
DEFAULT_CAPITAL_ALLOCATED = 500_000.0
DEFAULT_RISK_PER_TRADE_PCT = 0.005
DEFAULT_MAX_DAILY_LOSS_PCT = 0.02
DEFAULT_MAX_SESSION_TRADES = 6
DEFAULT_MAX_CONSECUTIVE_LOSSES = 3
DEFAULT_MAX_LOTS_PER_TRADE = 5

# Profile
RISK_PROFILE_AGGRESSIVE_SAFE_V1 = "aggressive_safe_v1"
