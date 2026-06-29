"""Config registry — the SINGLE place every strategy tunable is declared.

Each :class:`ConfigKey` maps a grouped YAML path (``exit.lottery.hard_stop_pct``)
to the env var the running code already reads (``LOTTERY_HARD_STOP_PCT``), plus
its type, default, group and whether the OPS sim may override it.

From this one table we *derive* (no more hand-maintained lists):
- the YAML -> env flattening (loader.py)
- ``ops_env.json`` keys (main.py)
- ``_SAFE_OVERRIDE_KEYS`` (ops_routes.py)
- the ``05_CONFIG_REFERENCE.md`` table (gen_config_docs.py, phase 4)

See ``docs/strategy_platform/CONFIG_CONSOLIDATION_PLAN.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ConfigType = Literal["str", "int", "float", "bool", "csv"]


@dataclass(frozen=True)
class ConfigKey:
    yaml_path: str          # dotted path into ops/strategy_config.yml
    env_var: str            # env var the running code reads (the 404 os.getenv sites)
    type: ConfigType
    default: Any            # used when absent from YAML
    group: str              # for docs / UI grouping
    description: str = ""
    sim_overridable: bool = False

    def format(self, value: Any) -> str:
        """Render a (yaml or default) value to the env-var string form code expects."""
        if value is None:
            return ""
        if self.type == "bool":
            if isinstance(value, str):
                truthy = value.strip().lower() in {"1", "true", "yes", "y", "on"}
            else:
                truthy = bool(value)
            return "1" if truthy else "0"
        if self.type == "csv":
            if isinstance(value, (list, tuple)):
                return ",".join(str(v).strip() for v in value)
            return str(value)
        if self.type == "int":
            try:
                return str(int(value))
            except (TypeError, ValueError):
                return str(value)
        if self.type == "float":
            return str(value)
        return str(value)


# ---------------------------------------------------------------------------
# THE REGISTRY
# ---------------------------------------------------------------------------
# Defaults below mirror the verified-live 2026-06-14 .env.compose values so the
# YAML can be generated from them and SIM matches LIVE. Phase 1 uses env_wins
# precedence, so these defaults cannot change live behaviour until phase 3.

REGISTRY: list[ConfigKey] = [
    # ----- execution ------------------------------------------------------
    ConfigKey("execution.adapter", "EXECUTION_ADAPTER", "str", "dhan", "execution",
              "paper | dhan | kite | shadow. Read by execution_app (not the loader "
              "yet); SIM forces read-only behaviour regardless."),
    ConfigKey("execution.rollout_stage", "rollout_stage", "str", "paper", "execution",
              "paper | live. Cosmetic — real orders gate on grade, not this."),

    # ----- profile --------------------------------------------------------
    ConfigKey("profile.id", "STRATEGY_PROFILE_ID", "str", "trader_master_live_v1", "profile",
              "Active strategy profile (router + risk config).", sim_overridable=True),

    # ----- entry ----------------------------------------------------------
    ConfigKey("entry.time_windows", "ENTRY_TIME_WINDOWS", "str", "09:45-14:30", "entry",
              "IST entry window(s).", sim_overridable=True),
    ConfigKey("entry.min_confidence", "STRATEGY_MIN_CONFIDENCE", "float", 0.65, "entry",
              "Base engine min confidence (non-consensus paths).", sim_overridable=True),
    ConfigKey("entry.bypass_min_confidence", "CONSENSUS_BYPASS_MIN_CONFIDENCE", "float", 0.65, "entry",
              "Min ML entry confidence for consensus-bypass path.", sim_overridable=True),
    ConfigKey("entry.pipeline_v2", "STRATEGY_ENTRY_PIPELINE_V2", "bool", False, "entry",
              "v2 gate-cascade entry pipeline. Off = legacy paths.", sim_overridable=True),
    ConfigKey("entry.vol_gate.enabled", "ENTRY_VOL_GATE_ENABLED", "bool", True, "entry",
              "1 = ATR vol gate trigger; 0 = entry_only_v3 ML model.", sim_overridable=True),
    ConfigKey("entry.vol_gate.atr_min_pct", "ATR_ENTRY_MIN_PCT", "float", 0.00088, "entry",
              "ATR gate: atr_14_1m / price >= this (~p90 live).", sim_overridable=True),
    ConfigKey("entry.ml.model_path", "ENTRY_ML_MODEL_PATH", "str",
              "/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model_020pct.joblib", "entry",
              "Entry ML bundle path (live = 020pct).", sim_overridable=True),
    ConfigKey("entry.ml.min_prob", "ENTRY_ML_MIN_PROB", "float", 0.45, "entry",
              "Min entry_only_v3 prob (only used when vol_gate.enabled=0).", sim_overridable=True),

    # ----- direction ------------------------------------------------------
    ConfigKey("direction.mode", "ML_ENTRY_DIRECTION_MODE", "str", "regime_dual", "direction",
              "composite | regime_dual | consensus. Live = regime_dual.", sim_overridable=True),
    ConfigKey("direction.ml.model_path", "DIRECTION_ML_MODEL_PATH", "str",
              "/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib", "direction",
              "Direction ML bundle path.", sim_overridable=True),
    ConfigKey("direction.ml.weight", "DIRECTION_ML_WEIGHT", "float", 0.40, "direction",
              "ML tilt weight in regime_dual (40% ML / 60% composite).", sim_overridable=True),
    ConfigKey("direction.ml.filter_min_prob", "DIRECTION_ML_FILTER_MIN_PROB", "str", "", "direction",
              "Optional hard ML prob filter. Blank = off.", sim_overridable=True),
    ConfigKey("direction.min_margin_sideways", "DIRECTION_MIN_MARGIN_SIDEWAYS", "float", 2.0, "direction",
              "Higher direction margin required in SIDEWAYS regime.", sim_overridable=True),

    # ----- regime ---------------------------------------------------------
    ConfigKey("regime.trend_score_min", "REGIME_TREND_SCORE_MIN", "float", 2.0, "regime",
              "Score needed to classify TRENDING (vs SIDEWAYS).", sim_overridable=True),
    ConfigKey("regime.aligned_bonus", "REGIME_TREND_ALIGNED_BONUS", "float", 0.0, "regime",
              "Bonus when 5/15/30m returns align. 0 = off.", sim_overridable=True),
    ConfigKey("regime.vol_ratio_min", "REGIME_TREND_VOL_RATIO_MIN", "float", 1.30, "regime",
              "vol_ratio threshold for strong_vol contribution.", sim_overridable=True),

    # ----- exit -----------------------------------------------------------
    ConfigKey("exit.mode", "EXIT_STRATEGY_MODE", "str", "adaptive", "exit",
              "scalper | adaptive (TRENDING/BREAKOUT->lottery, rest->scalper).", sim_overridable=True),
    ConfigKey("exit.max_loss_pct", "EXIT_MAX_LOSS_PCT", "float", 0.10, "exit",
              "Universal loss floor wrapping every mode.", sim_overridable=True),
    ConfigKey("exit.policy_stack_enabled", "EXIT_POLICY_STACK_ENABLED", "bool", True, "exit",
              "Master switch for the composite exit stack.", sim_overridable=True),
    # scalper sub-stack
    ConfigKey("exit.scalper.hard_stop_pct", "EXIT_SCALPER_HARD_STOP_PCT", "float", 0.07, "exit",
              "Scalper hard stop (% of entry).", sim_overridable=True),
    ConfigKey("exit.scalper.target_pct", "EXIT_PREMIUM_TARGET_PCT", "float", 0.03, "exit",
              "Scalper profit target (% of entry).", sim_overridable=True),
    ConfigKey("exit.scalper.trailing_activation_pct", "EXIT_TRAILING_ACTIVATION_PCT", "float", 0.015, "exit",
              "Trailing stop activates once MFE >= this.", sim_overridable=True),
    ConfigKey("exit.scalper.trailing_trail_pct", "EXIT_TRAILING_TRAIL_PCT", "float", 0.008, "exit",
              "Once active, exit on giveback of this from peak.", sim_overridable=True),
    ConfigKey("exit.scalper.thesis_fail_bars", "EXIT_THESIS_FAIL_BARS", "int", 999, "exit",
              "Cut flat scalper trade after N bars. 999 = disabled.", sim_overridable=True),
    ConfigKey("exit.scalper.thesis_fail_min_mfe", "EXIT_THESIS_FAIL_MIN_MFE", "float", 0.002, "exit",
              "'Hasn't moved' = MFE < this.", sim_overridable=True),
    # lottery sub-stack
    ConfigKey("exit.lottery.regimes", "ADAPTIVE_LOTTERY_REGIMES", "csv", ["BREAKOUT", "TRENDING"], "exit",
              "Regimes routed to the lottery stack in adaptive mode.", sim_overridable=True),
    ConfigKey("exit.lottery.hard_stop_pct", "LOTTERY_HARD_STOP_PCT", "float", 0.20, "exit",
              "Lottery loss cap (universal floor may fire first).", sim_overridable=True),
    ConfigKey("exit.lottery.big_target_pct", "LOTTERY_BIG_TARGET_PCT", "float", 0.50, "exit",
              "Lottery profit target.", sim_overridable=True),
    ConfigKey("exit.lottery.runner_activation_mfe", "LOTTERY_RUNNER_ACTIVATION_MFE", "float", 0.20, "exit",
              "Runner trail activates after MFE >= this.", sim_overridable=True),
    ConfigKey("exit.lottery.runner_giveback_frac", "LOTTERY_RUNNER_GIVEBACK_FRAC", "float", 0.35, "exit",
              "Once active, exit if pnl < peak*(1-this).", sim_overridable=True),
    ConfigKey("exit.lottery.thesis_fail_bars", "LOTTERY_THESIS_FAIL_BARS", "int", 999, "exit",
              "999 = disabled; timestop is the lottery backstop.", sim_overridable=True),
    ConfigKey("exit.lottery.thesis_fail_min_mfe", "LOTTERY_THESIS_FAIL_MIN_MFE", "float", 0.03, "exit",
              "Only relevant when thesis_fail_bars < 999.", sim_overridable=True),
    ConfigKey("exit.lottery.timestop_bars", "LOTTERY_TIMESTOP_BARS", "int", 90, "exit",
              "Max hold (min) — real lottery backstop.", sim_overridable=True),
    ConfigKey("exit.lottery.momentum_flip", "LOTTERY_MOMENTUM_FLIP", "float", 1.0, "exit",
              "Exit if shadow score flips by this magnitude. 0 = off.", sim_overridable=True),
    # giveback stop (both stacks)
    ConfigKey("exit.giveback.enabled", "EXIT_GIVEBACK_STOP_ENABLED", "bool", False, "exit",
              "Enable GivebackStopPolicy in scalper+lottery stacks (Jun-4 dead-zone fix).", sim_overridable=True),
    ConfigKey("exit.giveback.min_mfe", "EXIT_GIVEBACK_MIN_MFE", "float", 0.03, "exit",
              "GivebackStop activates once MFE >= this (scalper+lottery).", sim_overridable=True),
    ConfigKey("exit.giveback.scalper_pct", "EXIT_GIVEBACK_PCT", "float", 0.09, "exit",
              "Scalper giveback tolerance: exit if pnl < mfe - this.", sim_overridable=True),
    ConfigKey("exit.giveback.lottery_pct", "LOTTERY_GIVEBACK_PCT", "float", 0.15, "exit",
              "Lottery giveback tolerance: wider so genuine winners aren't choked.", sim_overridable=True),

    # ----- strike ---------------------------------------------------------
    ConfigKey("strike.policy", "STRATEGY_STRIKE_SELECTION_POLICY", "str", "otm", "strike",
              "atm | otm | smart_strike.", sim_overridable=True),
    ConfigKey("strike.smart_strike_enabled", "STRATEGY_SMART_STRIKE_ENABLED", "bool", True, "strike",
              "Master switch for smart-strike tiers.", sim_overridable=True),
    ConfigKey("strike.min_premium", "SMART_STRIKE_MIN_PREMIUM", "int", 0, "strike",
              "Premium floor (0 = no floor; matches live code default).", sim_overridable=True),
    ConfigKey("strike.max_premium", "SMART_STRIKE_MAX_PREMIUM", "int", 1300, "strike",
              "Don't buy more expensive than this.", sim_overridable=True),
    ConfigKey("strike.max_otm_steps", "STRATEGY_STRIKE_MAX_OTM_STEPS", "int", 12, "strike",
              "Max OTM depth in steps.", sim_overridable=True),
    ConfigKey("strike.otm.confidence", "SMART_STRIKE_OTM_CONFIDENCE", "float", 0.55, "strike",
              "Min confidence for OTM-1 tier."),
    ConfigKey("strike.otm2.enabled", "SMART_STRIKE_OTM2_ENABLED", "bool", True, "strike", "Enable OTM-2."),
    ConfigKey("strike.otm2.confidence", "SMART_STRIKE_OTM2_CONFIDENCE", "float", 0.65, "strike", "Min conf OTM-2."),
    ConfigKey("strike.otm3.enabled", "SMART_STRIKE_OTM3_ENABLED", "bool", True, "strike", "Enable OTM-3."),
    ConfigKey("strike.otm3.confidence", "SMART_STRIKE_OTM3_CONFIDENCE", "float", 0.75, "strike", "Min conf OTM-3."),
    ConfigKey("strike.otm3.regimes", "SMART_STRIKE_OTM3_REGIMES", "csv", ["BREAKOUT", "TRENDING"], "strike",
              "Regime restriction for OTM-3."),
    ConfigKey("strike.otm4.enabled", "SMART_STRIKE_OTM4_ENABLED", "bool", True, "strike", "Enable OTM-4."),
    ConfigKey("strike.otm4.confidence", "SMART_STRIKE_OTM4_CONFIDENCE", "float", 0.85, "strike", "Min conf OTM-4."),
    ConfigKey("strike.otm4.regimes", "SMART_STRIKE_OTM4_REGIMES", "csv", ["BREAKOUT"], "strike",
              "Regime restriction for OTM-4."),
    ConfigKey("strike.otm2.min_oi", "SMART_STRIKE_OTM2_MIN_OI", "int", 75000, "strike", "Liquidity floor OTM-2."),
    ConfigKey("strike.otm3.min_oi", "SMART_STRIKE_OTM3_MIN_OI", "int", 75000, "strike", "Liquidity floor OTM-3."),
    ConfigKey("strike.otm4.min_oi", "SMART_STRIKE_OTM4_MIN_OI", "int", 50000, "strike", "Liquidity floor OTM-4."),

    # ----- risk -----------------------------------------------------------
    ConfigKey("risk.max_consecutive_losses", "RISK_MAX_CONSECUTIVE_LOSSES", "int", 6, "risk",
              "Halt after N consecutive losses.", sim_overridable=True),
    ConfigKey("risk.max_session_trades", "RISK_MAX_SESSION_TRADES", "int", 6, "risk",
              "Max trades/day.", sim_overridable=True),
    ConfigKey("risk.max_lots_per_trade", "RISK_MAX_LOTS_PER_TRADE", "int", 1, "risk",
              "Hard lot cap (live = 1 lot)."),
    ConfigKey("risk.capital", "RISK_CAPITAL_ALLOCATED", "int", 41000, "risk",
              "Capital base for sizing (live Dhan balance)."),
    ConfigKey("risk.per_trade_pct", "RISK_PER_TRADE_PCT", "float", 0.005, "risk", "Fraction risked per trade."),
    ConfigKey("risk.live_min_grade", "RISK_LIVE_MIN_GRADE", "str", "OK", "risk",
              "Grade floor for live eligibility (GOOD | OK).", sim_overridable=True),
]


# ---------------------------------------------------------------------------
# Derived views (computed once)
# ---------------------------------------------------------------------------
BY_ENV: dict[str, ConfigKey] = {k.env_var: k for k in REGISTRY}
BY_YAML: dict[str, ConfigKey] = {k.yaml_path: k for k in REGISTRY}

# replaces the hand-maintained main.py list
OPS_ENV_KEYS: list[str] = [k.env_var for k in REGISTRY]

# replaces the hand-maintained ops_routes.py set
SAFE_OVERRIDE_KEYS: set[str] = {k.env_var for k in REGISTRY if k.sim_overridable}
