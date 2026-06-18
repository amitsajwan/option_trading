"""Shared deterministic strategy profile definitions."""

from __future__ import annotations

import os
from typing import Any

PROFILE_DET_PROD_V1 = "det_prod_v1"
PROFILE_DET_CORE_V2 = "det_core_v2"
PROFILE_DET_SETUP_V1 = "det_setup_v1"
PROFILE_DET_V3_V1 = "det_v3_v1"
PROFILE_R1S_TOP3_PAPER_V1 = "r1s_top3_paper_v1"
PROFILE_PLAYBOOK_V1_PAPER_V1 = "playbook_v1_paper_v1"
PROFILE_DEBIT_MULTI_V1 = "debit_multi_v1"
PROFILE_TRADER_MASTER_V1 = "trader_master_v1"
PROFILE_TRADER_MASTER_ML_ENTRY_V1 = "trader_master_ml_entry_v1"
PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1 = "trader_master_ml_entry_det_dir_v1"
# E4-S2: same as v1 but stagnant exit held until momentum reverses (shadow_score_crossed_zero).
PROFILE_TRADER_MASTER_ML_ENTRY_V1_DYN_EXIT = "trader_master_ml_entry_v1_dyn_exit"
PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20 = "trader_master_ml_entry_v1_stagnant_20"
PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20_DYN_EXIT = "trader_master_ml_entry_v1_stagnant_20_dyn_exit"
# ML timing + rule/shadow/momentum direction consensus; veto if unclear; ATM-only; fast thesis-fail exit.
PROFILE_TRADER_MASTER_ML_ENTRY_CONSENSUS_V1 = "trader_master_ml_entry_consensus_v1"
# Live trading: ML entry + CE-only + tighter session cap + depth signals enabled.
# Use with DEPTH_FEED_ENABLED=1 and ML_ENTRY_BLOCK_PE=1 env vars.
PROFILE_TRADER_MASTER_LIVE_V1 = "trader_master_live_v1"

PRODUCTION_DEFAULT_PROFILE_ID = PROFILE_DET_PROD_V1

_R1S_TOP3_ALL_REGIMES = ["R1S_TOP3_SHORT_CE"]
_R1S_TOP3_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": list(_R1S_TOP3_ALL_REGIMES),
    "SIDEWAYS": list(_R1S_TOP3_ALL_REGIMES),
    "EXPIRY": [],
    "PRE_EXPIRY": list(_R1S_TOP3_ALL_REGIMES),
    "HIGH_VOL": list(_R1S_TOP3_ALL_REGIMES),
    "AVOID": [],
}
_R1S_TOP3_RISK_CONFIG: dict[str, Any] = {
    "stop_loss_pct": 1.0,
    "target_pct": 0.5,
    "trailing_enabled": False,
}

_PLAYBOOK_V1_ALL_REGIMES = ["PBV1_TOP3_THESIS"]
_PLAYBOOK_V1_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": list(_PLAYBOOK_V1_ALL_REGIMES),
    "SIDEWAYS": list(_PLAYBOOK_V1_ALL_REGIMES),
    "EXPIRY": [],
    "PRE_EXPIRY": list(_PLAYBOOK_V1_ALL_REGIMES),
    "HIGH_VOL": list(_PLAYBOOK_V1_ALL_REGIMES),
    "AVOID": [],
}
# Exits owned by PlaybookBrain (rule JSON); do not override stop/target from profile.
_PLAYBOOK_V1_RISK_CONFIG: dict[str, Any] = {
    "trailing_enabled": False,
}

# Debit-only book: regime picks which long-option playbook runs (CE vs PE).
_DEBIT_MULTI_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": ["IV_FILTER", "R2_TOP3_LONG_CE"],
    "SIDEWAYS": ["IV_FILTER", "R1_TOP3_LONG_PE"],
    "EXPIRY": ["IV_FILTER"],
    "PRE_EXPIRY": ["IV_FILTER", "R2_TOP3_LONG_CE", "R1_TOP3_LONG_PE"],
    "HIGH_VOL": ["IV_FILTER"],
    "AVOID": [],
}
_DEBIT_MULTI_EXIT_STRATEGIES: list[str] = [
    "R1_TOP3_LONG_PE",
    "R2_TOP3_LONG_CE",
    "ORB",
    "OI_BUILDUP",
]
_DEBIT_MULTI_RISK_CONFIG: dict[str, Any] = {
    # Primary exit: close when premium PnL drops to -25%.
    "stop_loss_pct": 0.25,
    # Secondary hard floor: exit if BankNifty futures moves 0.20% against us (~100 pts).
    "underlying_stop_pct": 0.002,
    "target_pct": 0.60,
    # Trail profits once +15% MFE is hit; keep 8% room before locking.
    "trailing_enabled": True,
    "trailing_activation_pct": 0.15,
    "trailing_offset_pct": 0.08,
    "trailing_lock_breakeven": True,
    # Stagnation: if after 20 bars (≈20 min) we haven't gained 5%, exit — stop theta decay.
    "stagnant_exit_bars": 20,
    "stagnant_min_gain_pct": 0.05,
    # ORB guard: skip ORB entries when opening-range is wider than 250 pts.
    "orb_max_range_pts": 250.0,
}

# Master book for evaluation: experienced trader — all major playbooks, regime-routed.
# Union of det_core_v2 + det_prod + debit multi + trader composites + rule top-3 + R1S + PBV1.
_TRADER_MASTER_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": [
        "IV_FILTER",
        "ORB",
        "OI_BUILDUP",
        "PREV_DAY_LEVEL",
        "R2_TOP3_LONG_CE",
        "R1S_TOP3_SHORT_CE",
        "TRADER_COMPOSITE",
        "TRADER_V3_COMPOSITE",
        "PBV1_TOP3_THESIS",
    ],
    "SIDEWAYS": [
        "IV_FILTER",
        "VWAP_RECLAIM",
        "OI_BUILDUP",
        "R1_TOP3_LONG_PE",
        "R1S_TOP3_SHORT_CE",
        "TRADER_COMPOSITE",
        "TRADER_V3_COMPOSITE",
        "PBV1_TOP3_THESIS",
    ],
    "EXPIRY": [
        "IV_FILTER",
        "VWAP_RECLAIM",
        "TRADER_V3_COMPOSITE",
    ],
    "PRE_EXPIRY": [
        "IV_FILTER",
        "ORB",
        "OI_BUILDUP",
        "PREV_DAY_LEVEL",
        "VWAP_RECLAIM",
        "R1_TOP3_LONG_PE",
        "R2_TOP3_LONG_CE",
        "R1S_TOP3_SHORT_CE",
        "TRADER_COMPOSITE",
        "TRADER_V3_COMPOSITE",
        "PBV1_TOP3_THESIS",
    ],
    "HIGH_VOL": [
        "IV_FILTER",
        "HIGH_VOL_ORB",
        "TRADER_V3_COMPOSITE",
        "R1S_TOP3_SHORT_CE",
    ],
    "AVOID": [],
}
_TRADER_MASTER_EXIT_STRATEGIES: list[str] = [
    "ORB",
    "OI_BUILDUP",
    "HIGH_VOL_ORB",
    "VWAP_RECLAIM",
    "PREV_DAY_LEVEL",
    "R1_TOP3_LONG_PE",
    "R2_TOP3_LONG_CE",
    "TRADER_COMPOSITE",
    "TRADER_V3_COMPOSITE",
    "R1S_TOP3_SHORT_CE",
    "PBV1_TOP3_THESIS",
]
_TRADER_MASTER_RISK_CONFIG: dict[str, Any] = {
    # Premium stop: cut at 20% loss — ML-gated entries have clear thesis,
    # if wrong within first few bars there's no reason to hold to 25%+.
    "stop_loss_pct": 0.20,
    "target_pct": 0.70,
    # Trailing: activate only after +35% — the 5-min entry model expects a +12-25%
    # option move; activating below 35% fires during the predicted move window itself
    # and cuts runners that could reach the 70% target. At 35% we're above the
    # model's full expected range, so activation means genuine continued momentum.
    "trailing_enabled": True,
    "trailing_activation_pct": 0.35,
    "trailing_offset_pct": 0.08,
    "trailing_lock_breakeven": True,
    # Stagnation exit: if after 3 bars (~3 min) the trade hasn't reached +5%,
    # exit — ML entry expects a move within the first few bars; flat/losing = thesis
    # failed, theta is eating us. 3 bars matches EXIT_THESIS_FAIL_BARS so both
    # mechanisms agree on the same cut-off.
    "stagnant_exit_bars": 3,
    "stagnant_min_gain_pct": 0.05,
}

# E4-S2 experiment: same risk book, but stagnant exit gated on momentum reversal.
# Won't fire if shadow_score still agrees with trade direction — gives runners room.
_TRADER_MASTER_DYN_EXIT_RISK_CONFIG: dict[str, Any] = {
    **_TRADER_MASTER_RISK_CONFIG,
    "stagnant_exit_condition": "shadow_score_crossed_zero",
}

# E1: same entry book; give ML thesis more time before stagnant TIME_STOP.
_TRADER_MASTER_STAGNANT_20_RISK_CONFIG: dict[str, Any] = {
    **_TRADER_MASTER_RISK_CONFIG,
    "stagnant_exit_bars": 3,   # was 20; aligned to 3-bar cut
}

# E4: E1 stagnant window + E2 shadow-gated defer on profitable stagnation.
_TRADER_MASTER_STAGNANT_20_DYN_EXIT_RISK_CONFIG: dict[str, Any] = {
    **_TRADER_MASTER_STAGNANT_20_RISK_CONFIG,
    "stagnant_exit_condition": "shadow_score_crossed_zero",
}

# Live v1: tighter stop/trail params for real-capital execution.
# CE-only is enforced via env var ML_ENTRY_BLOCK_PE=1 (not in risk config).
# Depth signals are activated by DEPTH_FEED_ENABLED=1 env var.
# Half-size session cap vs paper until first live OOS window is complete.
_TRADER_MASTER_LIVE_V1_RISK_CONFIG: dict[str, Any] = {
    **_TRADER_MASTER_RISK_CONFIG,
    # Tighter stop: live fills have slippage; cut faster on wrong-side entries.
    "stop_loss_pct": 0.18,
    # Keep target same — no reason to cap winners in live.
    "target_pct": 0.70,
    # Trail earlier: protect capital once +25% MFE (vs 35% in paper).
    "trailing_activation_pct": 0.25,
    "trailing_offset_pct": 0.08,
    "trailing_lock_breakeven": True,
    # Stagnation: cut at 3 bars — if no move in 3 min, thesis is wrong.
    "stagnant_exit_bars": 3,
    "stagnant_min_gain_pct": 0.05,
    # Hard session cap: max 4 live trades per session until edge is re-verified.
    "session_trade_cap": 4,
    # ATM only for det-engine strategies; ML_ENTRY bypasses this when STRATEGY_SMART_STRIKE_ENABLED=1.
    "atm_strike_only": True,
    "allow_non_atm_for_ml_entry": True,
}

# Consensus direction: ML_ENTRY = timing only; exit fast when 5m thesis fails in first ~2 bars.
_TRADER_MASTER_ML_ENTRY_CONSENSUS_RISK_CONFIG: dict[str, Any] = {
    **_TRADER_MASTER_RISK_CONFIG,
    "underlying_stop_pct": 0.0015,
    "thesis_fail_exit_bars": 2,
    "thesis_fail_min_mfe_pct": 0.02,
    "thesis_fail_pnl_pct": -0.03,
    "early_stop_loss_bars": 2,
    "early_stop_loss_pct": 0.12,
    "atm_strike_only": True,
    "allow_non_atm_for_ml_entry": True,
}

# Same exit/risk book as trader_master; entry is ML_ENTRY only (+ IV_FILTER veto).
_TRADER_MASTER_ML_ENTRY_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    regime: ["IV_FILTER", "ML_ENTRY"]
    for regime, strategies in _TRADER_MASTER_REGIME_ENTRY_MAP.items()
    if strategies
}
_TRADER_MASTER_ML_ENTRY_REGIME_ENTRY_MAP["AVOID"] = []
# CHOP = no sustained directional move by definition. ML_ENTRY is a timing model
# that expects a move to develop; in CHOP that move never comes. The _NEW_REGIME_FALLBACKS
# map would route CHOP → SIDEWAYS (which has ML_ENTRY), so we override explicitly.
# Analysis: 3 consecutive PE losses in CHOP on 2026-06-03, all TIME_STOP at 3 bars,
# MFE=0.00% on 2 of 3 — market never moved. Root cause: CHOP entry should not happen.
_TRADER_MASTER_ML_ENTRY_REGIME_ENTRY_MAP["CHOP"] = []

# ML step-① timing + trader_master rule strategies for step-② direction (no direction ML).
_TRADER_MASTER_ML_ENTRY_DET_DIR_REGIME_ENTRY_MAP: dict[str, list[str]] = {}
for _regime, _strategies in _TRADER_MASTER_REGIME_ENTRY_MAP.items():
    if not _strategies:
        _TRADER_MASTER_ML_ENTRY_DET_DIR_REGIME_ENTRY_MAP[_regime] = []
        continue
    _merged: list[str] = ["IV_FILTER", "ML_ENTRY"]
    for _name in _strategies:
        if _name not in _merged:
            _merged.append(_name)
    _TRADER_MASTER_ML_ENTRY_DET_DIR_REGIME_ENTRY_MAP[_regime] = _merged

_DET_PROD_V1_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": ["IV_FILTER", "ORB", "OI_BUILDUP"],
    "SIDEWAYS": ["IV_FILTER", "OI_BUILDUP"],
    "EXPIRY": ["IV_FILTER"],
    "PRE_EXPIRY": ["IV_FILTER", "ORB", "OI_BUILDUP"],
    "HIGH_VOL": ["IV_FILTER", "HIGH_VOL_ORB"],
    "AVOID": [],
}

_DET_PROD_V1_EXIT_STRATEGIES: list[str] = ["ORB", "OI_BUILDUP", "HIGH_VOL_ORB"]

_DET_PROD_V1_RISK_CONFIG: dict[str, Any] = {
    "stop_loss_pct": 0.20,
    "target_pct": 0.80,
    "trailing_enabled": True,
    "trailing_activation_pct": 0.10,
    "trailing_offset_pct": 0.05,
    "trailing_lock_breakeven": True,
}

_DET_CORE_V2_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": ["IV_FILTER", "ORB", "OI_BUILDUP", "PREV_DAY_LEVEL"],
    "SIDEWAYS": ["IV_FILTER", "VWAP_RECLAIM", "OI_BUILDUP"],
    "EXPIRY": ["IV_FILTER", "VWAP_RECLAIM"],
    "PRE_EXPIRY": ["IV_FILTER", "ORB", "OI_BUILDUP"],
    "HIGH_VOL": ["IV_FILTER", "HIGH_VOL_ORB"],
    "AVOID": [],
}

_DET_CORE_V2_EXIT_STRATEGIES: list[str] = ["ORB", "VWAP_RECLAIM", "OI_BUILDUP"]

_DET_SETUP_V1_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": ["IV_FILTER", "TRADER_COMPOSITE"],
    "SIDEWAYS": ["IV_FILTER", "TRADER_COMPOSITE"],
    "EXPIRY": ["IV_FILTER"],
    "PRE_EXPIRY": ["IV_FILTER", "TRADER_COMPOSITE"],
    "HIGH_VOL": ["IV_FILTER", "TRADER_COMPOSITE"],
    "AVOID": [],
}

_DET_SETUP_V1_EXIT_STRATEGIES: list[str] = ["TRADER_COMPOSITE"]

_DET_SETUP_V1_RISK_CONFIG: dict[str, Any] = {
    "stop_loss_pct": 0.20,
    "target_pct": 0.80,
    "trailing_enabled": True,
    "trailing_activation_pct": 0.10,
    "trailing_offset_pct": 0.05,
    "trailing_lock_breakeven": True,
}

_DET_V3_V1_REGIME_ENTRY_MAP: dict[str, list[str]] = {
    "TRENDING": ["IV_FILTER", "TRADER_V3_COMPOSITE"],
    "SIDEWAYS": ["IV_FILTER", "TRADER_V3_COMPOSITE"],
    "EXPIRY": ["IV_FILTER", "TRADER_V3_COMPOSITE"],
    "PRE_EXPIRY": ["IV_FILTER", "TRADER_V3_COMPOSITE"],
    "HIGH_VOL": ["IV_FILTER", "TRADER_V3_COMPOSITE"],
    "AVOID": [],
}

_DET_V3_V1_EXIT_STRATEGIES: list[str] = ["TRADER_V3_COMPOSITE"]

_DET_V3_V1_RISK_CONFIG: dict[str, Any] = {
    "stop_loss_pct": 0.18,
    "target_pct": 0.65,
    "trailing_enabled": True,
    "trailing_activation_pct": 0.08,
    "trailing_offset_pct": 0.04,
    "trailing_lock_breakeven": True,
}

# Mapping from new Phase 3 regime labels to the existing label they inherit from.
# Applied after all per-profile maps are defined so adding new Regime enum members
# never requires editing every individual map.
_NEW_REGIME_FALLBACKS: dict[str, str] = {
    "CHOP": "SIDEWAYS",      # Chop uses same strategies as sideways
    "BREAKOUT": "TRENDING",  # Breakout uses same strategies as trending
    "PANIC": "AVOID",        # No new entries during panic
    "DEAD_MARKET": "AVOID",  # No new entries in dead markets
}

_PROFILE_REGIME_ENTRY_MAPS: dict[str, dict[str, list[str]]] = {
    PROFILE_DET_PROD_V1: _DET_PROD_V1_REGIME_ENTRY_MAP,
    PROFILE_DET_CORE_V2: _DET_CORE_V2_REGIME_ENTRY_MAP,
    PROFILE_DET_SETUP_V1: _DET_SETUP_V1_REGIME_ENTRY_MAP,
    PROFILE_DET_V3_V1: _DET_V3_V1_REGIME_ENTRY_MAP,
    PROFILE_R1S_TOP3_PAPER_V1: _R1S_TOP3_REGIME_ENTRY_MAP,
    PROFILE_PLAYBOOK_V1_PAPER_V1: _PLAYBOOK_V1_REGIME_ENTRY_MAP,
    PROFILE_DEBIT_MULTI_V1: _DEBIT_MULTI_REGIME_ENTRY_MAP,
    PROFILE_TRADER_MASTER_V1: _TRADER_MASTER_REGIME_ENTRY_MAP,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1: _TRADER_MASTER_ML_ENTRY_REGIME_ENTRY_MAP,
    PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1: _TRADER_MASTER_ML_ENTRY_DET_DIR_REGIME_ENTRY_MAP,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1_DYN_EXIT: _TRADER_MASTER_ML_ENTRY_REGIME_ENTRY_MAP,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20: _TRADER_MASTER_ML_ENTRY_REGIME_ENTRY_MAP,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20_DYN_EXIT: _TRADER_MASTER_ML_ENTRY_REGIME_ENTRY_MAP,
    PROFILE_TRADER_MASTER_ML_ENTRY_CONSENSUS_V1: _TRADER_MASTER_ML_ENTRY_DET_DIR_REGIME_ENTRY_MAP,
    PROFILE_TRADER_MASTER_LIVE_V1: _TRADER_MASTER_ML_ENTRY_REGIME_ENTRY_MAP,
}

# Backfill new regime labels into every profile map using the fallback mapping.
# This ensures StrategyRouter.get_strategies_for_regime() never KeyErrors on new labels.
for _profile_map in _PROFILE_REGIME_ENTRY_MAPS.values():
    for _new_label, _fallback in _NEW_REGIME_FALLBACKS.items():
        if _new_label not in _profile_map:
            _profile_map[_new_label] = list(_profile_map.get(_fallback, []))

_PROFILE_EXIT_STRATEGIES: dict[str, list[str]] = {
    PROFILE_DET_PROD_V1: _DET_PROD_V1_EXIT_STRATEGIES,
    PROFILE_DET_CORE_V2: _DET_CORE_V2_EXIT_STRATEGIES,
    PROFILE_DET_SETUP_V1: _DET_SETUP_V1_EXIT_STRATEGIES,
    PROFILE_DET_V3_V1: _DET_V3_V1_EXIT_STRATEGIES,
    PROFILE_R1S_TOP3_PAPER_V1: list(_R1S_TOP3_ALL_REGIMES),
    PROFILE_PLAYBOOK_V1_PAPER_V1: list(_PLAYBOOK_V1_ALL_REGIMES),
    PROFILE_DEBIT_MULTI_V1: list(_DEBIT_MULTI_EXIT_STRATEGIES),
    PROFILE_TRADER_MASTER_V1: list(_TRADER_MASTER_EXIT_STRATEGIES),
    PROFILE_TRADER_MASTER_ML_ENTRY_V1: list(_TRADER_MASTER_EXIT_STRATEGIES),
    PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1: list(_TRADER_MASTER_EXIT_STRATEGIES),
    PROFILE_TRADER_MASTER_ML_ENTRY_V1_DYN_EXIT: list(_TRADER_MASTER_EXIT_STRATEGIES),
    PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20: list(_TRADER_MASTER_EXIT_STRATEGIES),
    PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20_DYN_EXIT: list(_TRADER_MASTER_EXIT_STRATEGIES),
    PROFILE_TRADER_MASTER_ML_ENTRY_CONSENSUS_V1: list(_TRADER_MASTER_EXIT_STRATEGIES),
    PROFILE_TRADER_MASTER_LIVE_V1: list(_TRADER_MASTER_EXIT_STRATEGIES),
}

_PROFILE_RISK_CONFIGS: dict[str, dict[str, Any]] = {
    PROFILE_DET_PROD_V1: _DET_PROD_V1_RISK_CONFIG,
    PROFILE_DET_SETUP_V1: _DET_SETUP_V1_RISK_CONFIG,
    PROFILE_DET_V3_V1: _DET_V3_V1_RISK_CONFIG,
    PROFILE_R1S_TOP3_PAPER_V1: _R1S_TOP3_RISK_CONFIG,
    PROFILE_PLAYBOOK_V1_PAPER_V1: _PLAYBOOK_V1_RISK_CONFIG,
    PROFILE_DEBIT_MULTI_V1: _DEBIT_MULTI_RISK_CONFIG,
    PROFILE_TRADER_MASTER_V1: _TRADER_MASTER_RISK_CONFIG,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1: _TRADER_MASTER_RISK_CONFIG,
    PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1: _TRADER_MASTER_RISK_CONFIG,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1_DYN_EXIT: _TRADER_MASTER_DYN_EXIT_RISK_CONFIG,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20: _TRADER_MASTER_STAGNANT_20_RISK_CONFIG,
    PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20_DYN_EXIT: _TRADER_MASTER_STAGNANT_20_DYN_EXIT_RISK_CONFIG,
    PROFILE_TRADER_MASTER_ML_ENTRY_CONSENSUS_V1: _TRADER_MASTER_ML_ENTRY_CONSENSUS_RISK_CONFIG,
    PROFILE_TRADER_MASTER_LIVE_V1: _TRADER_MASTER_LIVE_V1_RISK_CONFIG,
}


def known_profile_ids() -> list[str]:
    return list(_PROFILE_REGIME_ENTRY_MAPS.keys())


def get_regime_entry_map(profile_id: str) -> dict[str, list[str]]:
    return {str(key): list(value) for key, value in _PROFILE_REGIME_ENTRY_MAPS[str(profile_id)].items()}


def get_exit_strategies(profile_id: str) -> list[str]:
    return list(_PROFILE_EXIT_STRATEGIES[str(profile_id)])


def get_risk_config(profile_id: str) -> dict[str, Any]:
    cfg = dict(_PROFILE_RISK_CONFIGS.get(str(profile_id), {}))
    env_thesis_pnl = os.getenv("THESIS_FAIL_PNL_PCT")
    if env_thesis_pnl is not None:
        try:
            cfg["thesis_fail_pnl_pct"] = float(env_thesis_pnl)
        except ValueError:
            pass
    return cfg


def build_router_config(profile_id: str) -> dict[str, Any]:
    return {
        "strategy_profile_id": str(profile_id),
        "regime_entry_map": get_regime_entry_map(profile_id),
        "exit_strategies": get_exit_strategies(profile_id),
    }


def build_run_metadata(profile_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "strategy_profile_id": str(profile_id),
        "router_config": build_router_config(profile_id),
    }
    risk_config = get_risk_config(profile_id)
    if risk_config:
        payload["risk_config"] = risk_config
    return payload


__all__ = [
    "PRODUCTION_DEFAULT_PROFILE_ID",
    "PROFILE_DET_CORE_V2",
    "PROFILE_DET_PROD_V1",
    "PROFILE_DET_SETUP_V1",
    "PROFILE_DET_V3_V1",
    "PROFILE_R1S_TOP3_PAPER_V1",
    "PROFILE_PLAYBOOK_V1_PAPER_V1",
    "PROFILE_DEBIT_MULTI_V1",
    "PROFILE_TRADER_MASTER_V1",
    "PROFILE_TRADER_MASTER_ML_ENTRY_V1",
    "PROFILE_TRADER_MASTER_ML_ENTRY_DET_DIR_V1",
    "PROFILE_TRADER_MASTER_ML_ENTRY_V1_DYN_EXIT",
    "PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20",
    "PROFILE_TRADER_MASTER_ML_ENTRY_V1_STAGNANT_20_DYN_EXIT",
    "PROFILE_TRADER_MASTER_ML_ENTRY_CONSENSUS_V1",
    "build_router_config",
    "build_run_metadata",
    "get_exit_strategies",
    "get_regime_entry_map",
    "get_risk_config",
    "known_profile_ids",
]
