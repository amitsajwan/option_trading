"""Live strategy-config auditor — the single source of truth for "what is actually running".

The behaviour of the live system is determined by ~180 env vars spread across
.env.compose / docker-compose.yml / the strategy profile, grouped by *prefix*
(ENTRY_, REGIME_, EXIT_, ...) rather than by *what they do*. That makes it easy
to (a) miss the actual thesis, and (b) trust a var that's been overridden by
another. This tool re-groups the config by the **trade pipeline** and flags
dead / overridden / ambiguous settings.

Usage:
    # against a running container's env:
    docker exec <strategy_app> printenv | python -m ops.config_audit -
    # or a file (one KEY=VALUE per line):
    python -m ops.config_audit /path/to/env.txt
    # or the current process env:
    python -m ops.config_audit

Each entry: (var, one-line description). The PIPELINE list IS the documentation —
keep it in lock-step with the code, and it can't go stale the way prose docs do.
"""
from __future__ import annotations

import sys
from typing import Callable, Dict, List, Optional, Tuple

# ── pipeline (ordered by how a trade actually flows) ──────────────────────────
# (stage_title, [(var, description), ...])
PIPELINE: List[Tuple[str, List[Tuple[str, str]]]] = [
    ("0. ENGINE / MASTER", [
        ("STRATEGY_ENGINE", "'deterministic' = intelligent brain (correct). 'ml_pure' = C1 futures model (RETIRED — overfit, inverts OOS)"),
        ("STRATEGY_PROFILE_ID", "selects the regime->entry-strategy map"),
        ("BRAIN_ENABLED", "master switch for the brain/senses layer"),
        ("MARKET_SESSION_ENABLED", "gate trading to market hours"),
        ("RISK_MAX_LOTS_PER_TRADE", "hard cap on size (1 = always 1 lot)"),
    ]),
    ("1. ENTRY TRIGGER (when)", [
        ("ENTRY_VOL_GATE_ENABLED", "1 = VOL_GATE_ENTRY replaces ML_ENTRY as the trigger"),
        ("ATR_ENTRY_MIN_PCT", "vol-gate: fire when atr_14_1m/price >= this"),
        ("ATR_ENTRY_MIN_ABS", "vol-gate: absolute-ATR override (0 = use pct)"),
        ("ATR_ENTRY_BB_MIN", "vol-gate: optional bb_width confirm (0 = off)"),
        ("ENTRY_ML_MODEL_PATH", "ML entry model (the trigger when vol-gate is OFF)"),
        ("ENTRY_ML_MIN_PROB", "ML entry fire threshold"),
        ("ENTRY_TIME_WINDOWS", "only enter inside these IST windows"),
        ("ENTRY_TIERING_ENABLED", "grade setup quality (ENTRY_QUALITY_*)"),
    ]),
    ("2. DIRECTION (which way)", [
        ("ML_ENTRY_DIRECTION_MODE", "how direction is resolved (regime_dual=live)"),
        ("BRAIN_DUAL_MODE", "regime_dual: shadow=log only, live=drives the order"),
        ("REGIME_DIRECTION_SIGNAL", "which detector picks CE/PE (weighted=graceful | combo/agreement_lever=hard-block-if-absent)"),
        ("REGIME_CONF_THRESHOLD", "weighted detector: min |net lean| to fire (else abstain)"),
        ("REGIME_ALLOWED", "direction QUALITY gate — TREND/MID/CHOP (NOT the Regime enum TRENDING/SIDEWAYS/BREAKOUT)"),
        ("REGIME_W_MOM", "weight for momentum_15m [ANTI-SIGNAL: set to 0 — 48.1% acc over 37k bars, inverts OOS]"),
        ("REGIME_W_VWAP", "weight for VWAP signal [50.5% acc, mildly anti — consider reducing]"),
        ("REGIME_W_MAXPAIN", "weight for max_pain [51.2% acc, H2 52.8%, positive contributor]"),
        ("REGIME_W_OI", "weight for ATM OI signal [52.1% acc, best individual — keep]"),
        ("REGIME_W_EMA", "weight for EMA trend signal [positive contributor — keep]"),
        ("DIRECTION_ML_MODEL_PATH", "optional direction-only ML model (direction_only_v2 RETIRED — inverts OOS)"),
        ("DIRECTION_ML_WEIGHT", "direction_ml_policy blend weight"),
        ("ENTRY_DIR_W_ML", "entry_direction_resolver ML tilt weight (separate path!)"),
        ("ML_ENTRY_CE_ONLY", "force CE"),
        ("ML_ENTRY_PE_ONLY", "force PE"),
    ]),
    ("3. STRIKE SELECTION", [
        ("STRATEGY_SMART_STRIKE_ENABLED", "1 = smart_strike OVERRIDES STRATEGY_STRIKE_SELECTION_POLICY"),
        ("SMART_STRIKE_MAX_PREMIUM", "smart_strike: premium cap"),
        ("SMART_STRIKE_OTM_CONFIDENCE", "smart_strike: confidence to step OTM"),
        ("STRATEGY_STRIKE_SELECTION_POLICY", "fallback strike policy (DEAD if smart_strike on)"),
        ("STRATEGY_STRIKE_MAX_OTM_STEPS", "fallback only (DEAD if smart_strike on)"),
    ]),
    ("4. EXIT (the asymmetry / edge)", [
        ("EXIT_STRATEGY_MODE", "adaptive = route exit policy by regime"),
        ("EXIT_POLICY_STACK_ENABLED", "enable the layered exit stack"),
        ("ADAPTIVE_LOTTERY_REGIMES", "regimes that get the LOTTERY (ride-the-winner) exit"),
        ("LOTTERY_BIG_TARGET_PCT", "lottery: ride for this gain (the big win)"),
        ("LOTTERY_HARD_STOP_PCT", "lottery stop (capped by EXIT_MAX_LOSS_PCT!)"),
        ("LOTTERY_TIMESTOP_BARS", "lottery: max hold (bars)"),
        ("LOTTERY_THESIS_FAIL_BARS", "lottery: cut if wrong after N bars"),
        ("EXIT_PREMIUM_TARGET_PCT", "scalper target (non-lottery regimes)"),
        ("EXIT_SCALPER_HARD_STOP_PCT", "scalper stop"),
        ("EXIT_THESIS_FAIL_BARS", "scalper: cut if wrong after N bars (cut-loss-fast)"),
        ("EXIT_MAX_LOSS_PCT", "UNIVERSAL max-loss floor — caps every mode's stop"),
    ]),
    ("5. RISK / SIZING", [
        ("RISK_CALCULATOR", "sizing method (fixed_fraction | ...)"),
        ("RISK_FRACTION_PCT", "fixed_fraction: fraction of capital risked"),
        ("RISK_PER_TRADE_PCT", "alt sizing knob (ambiguous vs RISK_FRACTION_PCT)"),
        ("RISK_CAPITAL_ALLOCATED", "capital base"),
        ("RISK_MAX_SESSION_TRADES", "max trades/session"),
        ("RISK_MAX_CONSECUTIVE_LOSSES", "halt after N losses"),
        ("RISK_MAX_DAILY_LOSS_PCT", "daily loss halt"),
        ("RISK_VIX_HALT_THRESHOLD", "halt above this VIX"),
        ("RISK_LIVE_MIN_GRADE", "min setup grade for a LIVE (real-money) order"),
    ]),
    ("6. SENSES (LLM / grounding)", [
        ("GROUNDING_ENABLED", "Gemini web-grounding session bias on/off"),
        ("GEMINI_WEB_MODEL", "grounding model"),
        ("BRAIN_CONSENSUS_REQUIRE_DIRECTION", "consensus gate (false = off)"),
    ]),
]

# ── conflict / dead-config rules: (label, predicate(env)->Optional[str]) ───────
def _is_on(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


CONFLICT_RULES: List[Tuple[str, Callable[[Dict[str, str]], Optional[str]]]] = [
    ("strike", lambda e: (
        "SMART_STRIKE on -> STRATEGY_STRIKE_SELECTION_POLICY=%s and STRATEGY_STRIKE_MAX_OTM_STEPS are DEAD"
        % e.get("STRATEGY_STRIKE_SELECTION_POLICY", "?")
        if _is_on(e.get("STRATEGY_SMART_STRIKE_ENABLED")) else None)),
    ("entry-trigger", lambda e: (
        "VOL_GATE on -> ENTRY_ML_MODEL_PATH / ENTRY_ML_MIN_PROB are DEAD as the *trigger* "
        "(but ML_ENTRY_DIRECTION_MODE still governs direction)"
        if _is_on(e.get("ENTRY_VOL_GATE_ENABLED")) else None)),
    ("lottery-stop", lambda e: (
        "EXIT_MAX_LOSS_PCT=%s caps LOTTERY_HARD_STOP_PCT=%s -> effective lottery stop is the smaller"
        % (e.get("EXIT_MAX_LOSS_PCT"), e.get("LOTTERY_HARD_STOP_PCT"))
        if e.get("EXIT_MAX_LOSS_PCT") and e.get("LOTTERY_HARD_STOP_PCT")
        and _f(e.get("EXIT_MAX_LOSS_PCT")) < _f(e.get("LOTTERY_HARD_STOP_PCT")) else None)),
    ("direction-ml", lambda e: (
        "regime_dual + BRAIN_DUAL_MODE=live -> the WEIGHTED regime detector drives direction; "
        "DIRECTION_ML_MODEL_PATH / DIRECTION_ML_WEIGHT=%s / ENTRY_DIR_W_ML=%s are all DEAD "
        "(consulted only in shadow / non-regime_dual modes)"
        % (e.get("DIRECTION_ML_WEIGHT"), e.get("ENTRY_DIR_W_ML"))
        if e.get("ML_ENTRY_DIRECTION_MODE", "").strip().lower() == "regime_dual"
        and e.get("BRAIN_DUAL_MODE", "").strip().lower() == "live" else None)),
    ("entry-window", lambda e: (
        "ENTRY_TIME_WINDOWS=%s set while ENTRY_WINDOW_START/END_IST empty -> the legacy window vars are unused"
        % e.get("ENTRY_TIME_WINDOWS")
        if e.get("ENTRY_TIME_WINDOWS") and not e.get("ENTRY_WINDOW_START_IST") else None)),
    ("risk-sizing", lambda e: (
        "RISK_MAX_LOTS_PER_TRADE=1 -> sizing knobs (RISK_FRACTION_PCT/RISK_PER_TRADE_PCT) only matter if >1 lot possible"
        if e.get("RISK_MAX_LOTS_PER_TRADE", "") == "1" else None)),
    ("adaptive-stop", lambda e: (
        "EXIT_STRATEGY_MODE=adaptive -> BREAKOUT/TRENDING entries use LOTTERY_HARD_STOP_PCT=%s, NOT EXIT_SCALPER_HARD_STOP_PCT=%s. "
        "If LOTTERY_HARD_STOP_PCT is unset/large, BREAKOUT entries have NO effective hard stop. Root cause of -6.1%% day (2026-06-05)."
        % (e.get("LOTTERY_HARD_STOP_PCT", "UNSET"), e.get("EXIT_SCALPER_HARD_STOP_PCT", "UNSET"))
        if e.get("EXIT_STRATEGY_MODE", "").strip().lower() == "adaptive" else None)),
    ("momentum-antisignal", lambda e: (
        "REGIME_W_MOM=%s is > 0 — momentum_15m is an ANTI-signal (48.1%% acc, inverts OOS to 43.9%%). Set REGIME_W_MOM=0."
        % e.get("REGIME_W_MOM")
        if _f(e.get("REGIME_W_MOM", "0")) > 0 else None)),
    ("direction-model-retired", lambda e: (
        "DIRECTION_ML_MODEL_PATH=%s points to a retired model (direction_only_v2 inverts OOS to 43.9%%). Remove this path."
        % e.get("DIRECTION_ML_MODEL_PATH")
        if (e.get("DIRECTION_ML_MODEL_PATH") or "").strip() != "" else None)),
    ("entry-model-missing", lambda e: (
        "STRATEGY_ENGINE=deterministic but ENTRY_ML_MODEL_PATH is empty and ENTRY_VOL_GATE_ENABLED is off. "
        "The vol gate / ML entry will be disabled — regime gate only. Set ENTRY_ML_MODEL_PATH to entry_only_v3."
        if e.get("STRATEGY_ENGINE", "").strip().lower() == "deterministic"
        and not (e.get("ENTRY_ML_MODEL_PATH") or "").strip()
        and not _is_on(e.get("ENTRY_VOL_GATE_ENABLED")) else None)),
]

# ── fatal rules: these abort startup if triggered ─────────────────────────────
# Anything in CONFLICT_RULES is a WARNING. Fatal rules are for configs that are
# guaranteed to produce wrong behaviour — running the wrong model, retired engine.
#
# fatal_errors(env) returns a list of strings; each is a blocker.
# main.py raises ValueError at startup if this list is non-empty.
_STALE_C1_RUN_IDS = ("staged_deep_hpo_c1",)

FATAL_RULES: List[Tuple[str, Callable[[Dict[str, str]], Optional[str]]]] = [
    ("engine-ml-pure-retired", lambda e: (
        "STRATEGY_ENGINE=ml_pure is RETIRED. The C1 model (ML_PURE_RUN_ID=%s) overfits 2024 and "
        "INVERTS to 43.9%% accuracy in 2026. Switch to STRATEGY_ENGINE=deterministic. "
        "This blocks startup to prevent trading on a known-dead model."
        % e.get("ML_PURE_RUN_ID", "")
        if e.get("STRATEGY_ENGINE", "").strip().lower() == "ml_pure"
        and any(s in (e.get("ML_PURE_RUN_ID") or "") for s in _STALE_C1_RUN_IDS) else None)),
    ("engine-ml-pure-no-model", lambda e: (
        "STRATEGY_ENGINE=ml_pure but ML_PURE_MODEL_PACKAGE is empty AND ML_PURE_RUN_ID is empty. "
        "The engine has no model to load and will fail at runtime. "
        "Either set STRATEGY_ENGINE=deterministic or provide a valid model package."
        if e.get("STRATEGY_ENGINE", "").strip().lower() == "ml_pure"
        and not (e.get("ML_PURE_MODEL_PACKAGE") or "").strip()
        and not (e.get("ML_PURE_RUN_ID") or "").strip() else None)),
]


def fatal_errors(env: Dict[str, str]) -> List[str]:
    """Return a list of fatal config errors that must block startup.

    Call this at process start. Raise ValueError with the returned messages
    so the operator sees the problem immediately rather than discovering it
    mid-session when something trades wrong.
    """
    errors: List[str] = []
    for label, rule in FATAL_RULES:
        try:
            msg = rule(env)
        except Exception:
            msg = None
        if msg:
            errors.append(f"[FATAL:{label}] {msg}")
    return errors


def _f(v: Optional[str]) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _parse_env(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def audit(env: Dict[str, str]) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("LIVE STRATEGY CONFIG — by trade pipeline (ground truth)")
    lines.append("=" * 78)
    for title, vars_ in PIPELINE:
        lines.append("")
        lines.append(title)
        for var, desc in vars_:
            val = env.get(var)
            shown = "(unset)" if val is None else (val if val != "" else "(empty)")
            lines.append(f"  {var:<34} = {shown:<30} | {desc}")
    lines.append("")
    lines.append("-" * 78)
    lines.append("CONFLICTS / DEAD / AMBIGUOUS (read these before trusting any single var):")
    found = False
    for label, rule in CONFLICT_RULES:
        try:
            msg = rule(env)
        except Exception:
            msg = None
        if msg:
            found = True
            lines.append(f"  [!] {label}: {msg}")
    if not found:
        lines.append("  (none detected)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "-":
        env = _parse_env(sys.stdin.read())
    elif argv:
        with open(argv[0]) as fh:
            env = _parse_env(fh.read())
    else:
        import os
        env = dict(os.environ)
    print(audit(env))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
