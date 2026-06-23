"""
System configuration verifier — run this to confirm the full live setup in one shot.

Usage (from runtime VM):
    sudo docker exec option_trading-strategy_app-1 python /app/ops/gcp/verify_config.py
    sudo docker exec option_trading-seller_app-1   python /app/ops/gcp/verify_config.py --seller

Also run automatically at container startup (logged to stdout so it appears in docker logs).
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path
from typing import Optional

# ── helpers ───────────────────────────────────────────────────────────────────
def _e(key: str, default: str = "<not set>") -> str:
    return os.environ.get(key, default) or default

def _ef(key: str, default: float = 0.0) -> float:
    try:    return float(os.environ.get(key) or default)
    except: return default

def _ok(cond: bool) -> str:
    return "✓" if cond else "✗ PROBLEM"

def _warn(cond: bool) -> str:
    return "✓" if cond else "⚠ CHECK"

def section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")

def row(label: str, value: str, status: str = "") -> None:
    pad = 32
    s   = f"  [{status}]" if status else ""
    print(f"  {label:<{pad}} {value}{s}")


def check_buyer(args) -> list[str]:
    problems = []

    section("BUYER — Entry model")
    model_path = _e("ENTRY_ML_MODEL_PATH")
    exists     = Path(model_path).exists() if model_path != "<not set>" else False
    row("ENTRY_ML_MODEL_PATH", model_path, _ok(exists))
    if not exists:
        problems.append(f"Entry model file missing: {model_path}")

    # ML threshold is a SELECTION FLOOR (arm B): model base rate is 6.2%, so 0.35
    # = ~6x base = a strong magnitude signal. The cost-ratio gate (below) does the
    # real precision work. 0.30-0.50 is the sane floor band.
    threshold = _ef("ENTRY_ML_MIN_PROB", 0.55)
    row("ENTRY_ML_MIN_PROB (selection floor)", f"{threshold:.2f}",
        _warn(0.30 <= threshold <= 0.50))
    if not (0.30 <= threshold <= 0.50):
        problems.append(f"ENTRY_ML_MIN_PROB={threshold} outside expected 0.30-0.50")

    # Data-readiness gate: abstain during feature warmup (NaN compression features
    # at the open produce inflated median-filled probs). 3 = allow ≤2 structural NaN.
    mnf = _e("ENTRY_ML_MAX_NAN_FEATURES", "<not set — warmup-artifact risk>")
    row("ENTRY_ML_MAX_NAN_FEATURES", mnf, _warn(mnf.isdigit()))
    if not mnf.isdigit():
        problems.append("ENTRY_ML_MAX_NAN_FEATURES not set — model may fire on warmup-artifact probs (set 3)")

    section("BUYER — Cost-ratio gate (arm B — the precision lever)")
    cg_on = _e("ENTRY_COST_RATIO_GATE_ENABLED", "1 (default on)")
    row("ENTRY_COST_RATIO_GATE_ENABLED", cg_on, _warn(cg_on not in ("0", "false")))
    row("ENTRY_COST_RATIO_MIN", _e("ENTRY_COST_RATIO_MIN", "1.5 (default)"))
    row("ENTRY_COST_HOLD_BARS", _e("ENTRY_COST_HOLD_BARS", "10 (default)"))
    slip = _e("ENTRY_COST_SLIPPAGE_PCT", "0.008 (placeholder)")
    row("ENTRY_COST_SLIPPAGE_PCT", slip)
    if "placeholder" in slip or _e("DEPTH_FEED_ENABLED", "0") not in ("1", "true"):
        row("  slippage source", "FLAT PLACEHOLDER — replace once depth feed measures bid-ask", "⚠ CHECK")

    if exists:
        try:
            import joblib
            bundle = joblib.load(model_path)
            rec    = bundle.get("recommended_min_prob", "?")
            kind   = bundle.get("kind", "?")
            feats  = len(bundle.get("features") or [])
            auc    = (bundle.get("holdout_eval") or {}).get("roc_auc", "?")
            row("  model kind",     kind)
            row("  features",       str(feats))
            row("  holdout AUC",    str(auc))
            row("  recommended_thr",str(rec))
        except Exception as e:
            row("  load error", str(e), "✗ PROBLEM")
            problems.append(f"Cannot load entry model: {e}")

    section("BUYER — Exit policy")
    exit_mode = _e("EXIT_STRATEGY_MODE", "scalper")
    row("EXIT_STRATEGY_MODE", exit_mode, _warn(exit_mode in ("adaptive","lottery")))
    if exit_mode not in ("adaptive", "lottery"):
        problems.append(f"EXIT_STRATEGY_MODE={exit_mode} — for compression entries, adaptive or lottery expected")

    if exit_mode == "adaptive":
        lottery_regimes = _e("ADAPTIVE_LOTTERY_REGIMES", "BREAKOUT,TRENDING (default)")
        row("ADAPTIVE_LOTTERY_REGIMES", lottery_regimes,
            _warn("TREND" in lottery_regimes or "TRENDING" in lottery_regimes))
        if "TREND" not in lottery_regimes and "TRENDING" not in lottery_regimes:
            problems.append("ADAPTIVE_LOTTERY_REGIMES should include TREND or TRENDING for compression entries")

    row("EXIT_SCALPER_HARD_STOP_PCT",   _e("EXIT_SCALPER_HARD_STOP_PCT",   "0.25 (default)"))
    row("EXIT_PREMIUM_TARGET_PCT",      _e("EXIT_PREMIUM_TARGET_PCT",       "0.04 (default)"))
    row("EXIT_TRAILING_ACTIVATION_PCT", _e("EXIT_TRAILING_ACTIVATION_PCT",  "0.01 (default)"))
    row("EXIT_TRAILING_TRAIL_PCT",      _e("EXIT_TRAILING_TRAIL_PCT",       "0.005 (default)"))
    row("EXIT_THESIS_FAIL_BARS",        _e("EXIT_THESIS_FAIL_BARS",         "3 (default)"))
    row("LOTTERY_HARD_STOP_PCT",        _e("LOTTERY_HARD_STOP_PCT",         "0.20 (default)"))
    row("LOTTERY_BIG_TARGET_PCT",       _e("LOTTERY_BIG_TARGET_PCT",        "0.50 (default)"))

    section("BUYER — Regime & direction")
    regime_sig = _e("REGIME_DIRECTION_SIGNAL", "combo")
    row("REGIME_DIRECTION_SIGNAL", regime_sig)
    row("REGIME_ALLOWED",          _e("REGIME_ALLOWED", "MID,TREND (default)"))
    row("REGIME_CONF_THRESHOLD",   _e("REGIME_CONF_THRESHOLD", "0.50"))

    section("BUYER — Risk & sizing")
    capital    = _ef("RISK_CAPITAL_ALLOCATED", 0)
    lot_size   = _ef("BANKNIFTY_LOT_SIZE", 30)
    min_grade  = _e("RISK_LIVE_MIN_GRADE", "OK")
    row("RISK_CAPITAL_ALLOCATED",  f"₹{capital:,.0f}", _warn(capital >= 20000))
    row("BANKNIFTY_LOT_SIZE",      str(int(lot_size)), _ok(lot_size in (15, 30)))
    row("RISK_LIVE_MIN_GRADE",     min_grade)
    row("RISK_MAX_DAILY_LOSS_PCT", _e("RISK_MAX_DAILY_LOSS_PCT", "0.12"))
    row("RISK_MAX_SESSION_TRADES", _e("RISK_MAX_SESSION_TRADES", "20"))

    section("BUYER — Direction (LIVE: multi_signal, veto-only)")
    dir_mode = _e("ML_ENTRY_DIRECTION_MODE", "composite (default)")
    row("ML_ENTRY_DIRECTION_MODE", dir_mode, _warn(dir_mode == "multi_signal"))
    if dir_mode != "multi_signal":
        problems.append(f"ML_ENTRY_DIRECTION_MODE={dir_mode!r} — live design is 'multi_signal' (6-signal stateless scorer, abstains when weak)")
    row("ENTRY_MULTI_SIGNAL_MIN", _e("ENTRY_MULTI_SIGNAL_MIN", "2.0 (default)"))
    # REGIME_DIRECTION_SIGNAL is DEAD for multi_signal — only regime_dual reads it.
    rds = _e("REGIME_DIRECTION_SIGNAL", "<not set>")
    if rds != "<not set>" and dir_mode == "multi_signal":
        row("REGIME_DIRECTION_SIGNAL", f"{rds}  (DEAD — only regime_dual uses it)", "⚠ CHECK")

    return problems


def check_seller(args) -> list[str]:
    problems = []

    section("SELLER — Core config")
    live_enabled = _e("SELLER_LIVE_ENABLED", "0")
    row("SELLER_LIVE_ENABLED", live_enabled,
        _warn(live_enabled in ("0","1")))

    row("SELLER_CONDOR_OFFSET",  _e("SELLER_CONDOR_OFFSET",  "200"))
    row("SELLER_SPREAD_WIDTH",   _e("SELLER_SPREAD_WIDTH",   "300"))
    row("SELLER_TP_FRAC",        _e("SELLER_TP_FRAC",        "0.50"))
    row("SELLER_STOP_MULT",      _e("SELLER_STOP_MULT",      "2.0"))
    row("SELLER_MAX_HOLD_DAYS",  _e("SELLER_MAX_HOLD_DAYS",  "5"))
    row("SELLER_IV_RANK_MIN",    _e("SELLER_IV_RANK_MIN",    "30"))
    row("SELLER_ENTRY_WINDOW",   _e("SELLER_ENTRY_WINDOW",   "10:00-14:00"))

    section("SELLER — State files")
    run_dir = _e("SELLER_RUN_DIR", "/seller_run")
    open_f  = Path(run_dir) / "seller_open_spreads.json"
    log_f   = Path(run_dir) / "seller_trades.jsonl"
    row("SELLER_RUN_DIR", run_dir, _ok(Path(run_dir).is_dir()))
    row("  open spreads file", str(open_f),
        "exists" if open_f.exists() else "not yet (no open trades)")
    row("  trades log", str(log_f),
        "exists" if log_f.exists() else "not yet (no trades)")
    row("  dir writable", str(Path(run_dir).is_dir()),
        _ok(os.access(run_dir, os.W_OK)))
    if not os.access(run_dir, os.W_OK):
        problems.append(f"Seller run dir not writable: {run_dir} — fix: chmod 777")

    return problems


def _print_feature_health() -> None:
    """Pull the latest live snapshot from Mongo and print its feature-health board."""
    import os as _os
    from pymongo import MongoClient

    from strategy_app.diagnostics import feature_health, format_report

    host = _os.environ.get("MONGO_HOST", "mongo")
    port = int(_os.environ.get("MONGO_PORT", "27017"))
    db = MongoClient(host, port, serverSelectionTimeoutMS=3000)["trading_ai"]
    doc = db["phase1_market_snapshots"].find_one(sort=[("_id", -1)])
    if not doc:
        print("  no snapshot found in trading_ai.phase1_market_snapshots")
        return
    # Live snapshots nest the payload under .payload.snapshot
    snap = (doc.get("payload") or {}).get("snapshot") or doc.get("snapshot") or doc
    print(format_report(feature_health(snap)))


def check_common() -> list[str]:
    problems = []

    section("EXECUTION & ADAPTER")
    adapter = _e("EXECUTION_ADAPTER", "paper")
    row("EXECUTION_ADAPTER",    adapter, _warn(adapter in ("dhan","paper")))
    row("ROLLOUT_STAGE",        _e("ROLLOUT_STAGE", "paper"))
    row("RISK_LIVE_MIN_GRADE",  _e("RISK_LIVE_MIN_GRADE", "OK"))

    section("INFRASTRUCTURE")
    mongo = _e("MONGO_HOST", "mongo")
    redis = _e("REDIS_HOST", "redis")
    row("MONGO_HOST", mongo)
    row("REDIS_HOST", redis)
    row("STRATEGY_REDIS_PUBLISH_ENABLED", _e("STRATEGY_REDIS_PUBLISH_ENABLED", "1"))
    row("INGESTION_COLLECTORS_ENABLED",   _e("INGESTION_COLLECTORS_ENABLED", "1"))
    row("TZ / MARKET_TIMEZONE",
        f"{_e('TZ')} / {_e('MARKET_TIMEZONE')}")

    section("LIVE FEATURE HEALTH (latest snapshot — what data is flowing)")
    try:
        _print_feature_health()
    except Exception as e:
        row("feature health", f"unavailable ({e})", "⚠ CHECK")

    section("DEPTH FEED (bid-ask — for real cost calibration)")
    depth_on = _e("DEPTH_FEED_ENABLED", "0")
    row("DEPTH_FEED_ENABLED", depth_on, _warn(depth_on in ("1", "true")))
    instruments = _e("DEPTH_FEED_INSTRUMENTS", "")
    if instruments in ("", "<not set>"):
        row("DEPTH_FEED_INSTRUMENTS", "<empty> — collector sleeps, NO depth captured", "⚠ CHECK")
        problems.append("DEPTH_FEED_INSTRUMENTS empty — set to current ATM CE/PE symbols (changes daily with spot + expiry)")
    else:
        row("DEPTH_FEED_INSTRUMENTS", instruments)
        row("  reminder", "must match TODAY'S ATM strike + ACTIVE expiry (stale = wrong depth)", "⚠ CHECK")

    section("VALIDATED FINDINGS (as of 2026-06-20)")
    print("  Entry model:   entry_compression_v1  (AUC 0.82, base rate 6.2%, well-calibrated)")
    print("  ML floor:      0.35 selection floor (NOT a high gate — 6x base rate)")
    print("  Cost gate:     arm B — drops moves that can't clear ~1.3% all-in cost (the real lever)")
    print("  Direction:     multi_signal, 6-signal stateless, abstains when weak (veto-only)")
    print("  Exit policy:   adaptive = TREND/TRENDING/BREAKOUT→lottery / SIDEWAYS→scalper")
    print("  Seller:        S3 iron condor, PAPER (SELLER_LIVE_ENABLED=0)")
    print()
    print("  OPEN ITEMS:")
    print("  - Depth: set DEPTH_FEED_INSTRUMENTS to today's ATM CE/PE; replace flat slippage placeholder")
    print("  - Seller live gate: set SELLER_LIVE_ENABLED=1 when ready")
    print("  - Futures rollover: update BANKNIFTY_FUTURES_SYMBOL before expiry (26JUN→26JUL)")

    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify live system config")
    parser.add_argument("--seller", action="store_true",
                        help="Check seller_app config (run from seller_app container)")
    parser.add_argument("--buyer",  action="store_true",
                        help="Check buyer/strategy_app config")
    parser.add_argument("--all",    action="store_true",
                        help="Check both buyer and seller (only if both volumes are mounted)")
    args = parser.parse_args()

    # default: buyer only — seller_run is not mounted in strategy_app
    if not args.seller and not args.buyer and not args.all:
        args.buyer = True
    if args.all:
        args.seller = True
        args.buyer  = True

    print("=" * 60)
    print("  SYSTEM CONFIG VERIFICATION")
    print(f"  Container: {os.environ.get('HOSTNAME', 'unknown')}")
    print("=" * 60)

    problems: list[str] = []
    problems += check_common()
    if args.buyer:
        problems += check_buyer(args)
    if args.seller:
        problems += check_seller(args)

    print(f"\n{'='*60}")
    if problems:
        print(f"  ✗ {len(problems)} PROBLEM(S) FOUND:")
        for p in problems:
            print(f"    • {p}")
    else:
        print("  ✓ All checks passed")
    print("=" * 60)


if __name__ == "__main__":
    main()
