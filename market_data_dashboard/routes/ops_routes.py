"""OPS tab backend — daily sim runner + live config reader.

Endpoints:
    GET  /api/ops/config          current live config (from runtime_config.json + env)
    POST /api/ops/sim/today       start today's sim with optional overrides → {job_id}
    GET  /api/ops/sim/{job_id}    poll progress + results
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

STRATEGY_RUN_DIR = Path(os.getenv("STRATEGY_RUN_DIR", "/app/.run/strategy_app"))
EVENTS_JSONL      = STRATEGY_RUN_DIR.parent / "snapshot_app" / "events.jsonl"

# Keys the UI is allowed to override for a sim run
_SAFE_OVERRIDE_KEYS = {
    "EXIT_POLICY_STACK_ENABLED",
    "EXIT_PREMIUM_TARGET_PCT",
    "EXIT_TRAILING_ACTIVATION_PCT",
    "EXIT_TRAILING_TRAIL_PCT",
    "EXIT_THESIS_FAIL_BARS",
    "EXIT_THESIS_FAIL_MIN_MFE",
    "CONSENSUS_BYPASS_MIN_CONFIDENCE",
    "STRATEGY_MIN_CONFIDENCE",
    "DIRECTION_MIN_MARGIN_SIDEWAYS",
    "STRATEGY_STRIKE_SELECTION_POLICY",
    "STRATEGY_SMART_STRIKE_ENABLED",
    "SMART_STRIKE_MAX_PREMIUM",
    "SMART_STRIKE_HARD_PREMIUM_CAP",
    "STRATEGY_STRIKE_MAX_OTM_STEPS",
    "RISK_MAX_CONSECUTIVE_LOSSES",
    "RISK_MAX_SESSION_TRADES",
    "STRATEGY_PROFILE_ID",
    "STRATEGY_ENTRY_PIPELINE_V2",
    "SMART_STRIKE_MIN_PREMIUM",
    # Lottery / adaptive mode
    "EXIT_STRATEGY_MODE",
    "ADAPTIVE_LOTTERY_REGIMES",
    "LOTTERY_HARD_STOP_PCT",
    "LOTTERY_BIG_TARGET_PCT",
    "LOTTERY_RUNNER_ACTIVATION_MFE",
    "LOTTERY_RUNNER_GIVEBACK_FRAC",
    "LOTTERY_THESIS_FAIL_BARS",
    "LOTTERY_MOMENTUM_FLIP",
    "LOTTERY_TIMESTOP_BARS",
}

# Live-job registry — keyed by job_id
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


# ── Pydantic models ────────────────────────────────────────────────────────────

class SimTodayRequest(BaseModel):
    date: Optional[str] = None          # defaults to today
    overrides: dict[str, str] = {}       # env var overrides for this sim only


# ── Config reader ──────────────────────────────────────────────────────────────

def _read_live_config() -> dict[str, Any]:
    """Return current live config from ops_env.json (written by strategy_app) + runtime_config."""
    cfg: dict[str, Any] = {}

    # Read runtime_config.json (engine + profile info)
    rc_path = STRATEGY_RUN_DIR / "runtime_config.json"
    if rc_path.exists():
        try:
            cfg = json.loads(rc_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # ops_env.json: written by strategy_app at startup with its actual env vars.
    # This is the source of truth for live config — not the dashboard's own os.environ.
    ops_env_from_file: dict[str, str] = {}
    ops_path = STRATEGY_RUN_DIR / "ops_env.json"
    if ops_path.exists():
        try:
            ops_env_from_file = json.loads(ops_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _e(key: str, fallback: str = "") -> str:
        # Prefer ops_env.json (strategy_app's actual value), fall back to dashboard's env
        if key in ops_env_from_file and ops_env_from_file[key]:
            return ops_env_from_file[key]
        return str(os.getenv(key, fallback) or fallback)

    ops_env = {
        "EXIT_POLICY_STACK_ENABLED":      _e("EXIT_POLICY_STACK_ENABLED", "0"),
        "EXIT_PREMIUM_TARGET_PCT":        _e("EXIT_PREMIUM_TARGET_PCT", "0.04"),
        "EXIT_TRAILING_ACTIVATION_PCT":   _e("EXIT_TRAILING_ACTIVATION_PCT", "0.01"),
        "EXIT_TRAILING_TRAIL_PCT":        _e("EXIT_TRAILING_TRAIL_PCT", "0.005"),
        "EXIT_THESIS_FAIL_BARS":          _e("EXIT_THESIS_FAIL_BARS", "3"),
        "EXIT_THESIS_FAIL_MIN_MFE":       _e("EXIT_THESIS_FAIL_MIN_MFE", "0.002"),
        "CONSENSUS_BYPASS_MIN_CONFIDENCE":_e("CONSENSUS_BYPASS_MIN_CONFIDENCE", "0.65"),
        "DIRECTION_MIN_MARGIN_SIDEWAYS":  _e("DIRECTION_MIN_MARGIN_SIDEWAYS", "2.0"),
        "STRATEGY_STRIKE_SELECTION_POLICY":_e("STRATEGY_STRIKE_SELECTION_POLICY", "atm"),
        "SMART_STRIKE_MIN_PREMIUM":       _e("SMART_STRIKE_MIN_PREMIUM", "600"),
        "SMART_STRIKE_MAX_PREMIUM":       _e("SMART_STRIKE_MAX_PREMIUM", "1300"),
        "STRATEGY_STRIKE_MAX_OTM_STEPS":  _e("STRATEGY_STRIKE_MAX_OTM_STEPS", "4"),
        "RISK_MAX_CONSECUTIVE_LOSSES":    _e("RISK_MAX_CONSECUTIVE_LOSSES", "3"),
        "RISK_MAX_SESSION_TRADES":        _e("RISK_MAX_SESSION_TRADES", "20"),
        "STRATEGY_PROFILE_ID":            _e("STRATEGY_PROFILE_ID", "trader_master_ml_entry_consensus_v1"),
        "EXIT_STRATEGY_MODE":             _e("EXIT_STRATEGY_MODE", "adaptive"),
        # Lottery params — ATM-tuned values (used for BREAKOUT/TRENDING in adaptive mode)
        "LOTTERY_HARD_STOP_PCT":          _e("LOTTERY_HARD_STOP_PCT", "0.15"),
        "LOTTERY_BIG_TARGET_PCT":         _e("LOTTERY_BIG_TARGET_PCT", "0.40"),
        "LOTTERY_RUNNER_ACTIVATION_MFE":  _e("LOTTERY_RUNNER_ACTIVATION_MFE", "0.15"),
        "LOTTERY_RUNNER_GIVEBACK_FRAC":   _e("LOTTERY_RUNNER_GIVEBACK_FRAC", "0.30"),
        "LOTTERY_THESIS_FAIL_BARS":       _e("LOTTERY_THESIS_FAIL_BARS", "5"),
        "LOTTERY_TIMESTOP_BARS":          _e("LOTTERY_TIMESTOP_BARS", "60"),
    }
    cfg["ops_env"] = ops_env
    cfg["strategy_run_dir"] = str(STRATEGY_RUN_DIR)
    cfg["events_jsonl_exists"] = EVENTS_JSONL.exists()
    return cfg


# ── Today's snapshot loader ────────────────────────────────────────────────────

def _load_today_snapshots(trade_date: str) -> list[dict]:
    if not EVENTS_JSONL.exists():
        return []
    snaps = []
    for line in EVENTS_JSONL.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            snap = d.get("snapshot", d)
            if str(snap.get("trade_date", "")).startswith(trade_date):
                snaps.append(snap)
        except Exception:
            pass
    return snaps


# ── Also load actual today's closed trades from positions JSONL ───────────────

def _hhmm(ts: str) -> str:
    return ts[11:16] if len(ts) > 15 else "?"


def _load_actual_trades(trade_date: str) -> list[dict]:
    """Reconstruct today's real closed trades from positions.jsonl.

    Two correctness fixes vs the naive version:
      1. Entry time comes from POSITION_OPEN, exit time from POSITION_CLOSE
         (previously both used the close timestamp → time_in == time_out).
      2. Restarts during the day append duplicate positions for the same
         logical trade. We dedupe by entry_snapshot_id (the bar a trade
         entered on uniquely identifies it), keeping the first occurrence —
         so the 6× repeated rows collapse to one.
    """
    pos_path = STRATEGY_RUN_DIR / "positions.jsonl"
    if not pos_path.exists():
        return []

    open_ts: dict[str, str] = {}      # position_id -> entry timestamp (from OPEN)
    closes: dict[str, dict] = {}       # position_id -> close record

    for line in pos_path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        # Only consider events whose timestamp is on the requested trade date
        ts = str(d.get("timestamp", ""))
        if not ts.startswith(trade_date):
            continue
        # Exclude sim runs that may have leaked into the live positions file.
        # Real live trades have run_id None (pre-run_id-fix) or "paper-*"/"capped_live-*".
        # Anything starting with "sim" is an OPS/standalone sim and must not appear
        # in the Actual panel.
        run_id = str(d.get("run_id") or "")
        if run_id.lower().startswith("sim"):
            continue
        pid = d.get("position_id", "")
        evt = d.get("event", "")
        if evt == "POSITION_OPEN" and pid and pid not in open_ts:
            open_ts[pid] = ts
        elif evt == "POSITION_CLOSE" and pid:
            closes[pid] = d  # last close per pid wins

    trades = []
    seen_keys: set = set()
    for pid, p in closes.items():
        close_ts = str(p.get("timestamp", ""))
        entry_ts = open_ts.get(pid, close_ts)
        # Dedup key: a logical trade = (entry bar, direction, strike, entry premium)
        dedup_key = (
            entry_ts[:16],
            str(p.get("direction", "")),
            p.get("strike"),
            round(float(p.get("entry_premium") or 0), 1),
        )
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        exit_r = p.get("exit_reason", "")
        label = str(p.get("exit_policy_triggered") or exit_r or "")
        trades.append({
            "time_in":   _hhmm(entry_ts),
            "time_out":  _hhmm(close_ts),
            "direction": p.get("direction", ""),
            "strike":    p.get("strike"),
            "prem_in":   float(p.get("entry_premium") or 0),
            "prem_out":  float(p.get("exit_premium") or p.get("entry_premium") or 0),
            "pnl_pct":   float(p.get("pnl_pct") or 0),
            "mfe_pct":   float(p.get("mfe_pct") or 0),
            "mae_pct":   float(p.get("mae_pct") or 0),
            "bars":      int(p.get("bars_held") or 0),
            "exit":      label,
            "source":    "actual",
        })
    trades.sort(key=lambda x: x["time_in"])
    return trades


# ── Sim runner ─────────────────────────────────────────────────────────────────

_ENV_LOCK = threading.Lock()


def _summarize_trades(trades: list[dict]) -> dict[str, Any]:
    """Aggregate a list of sim/actual trades into the OPS summary chips.

    Capture ratio is the AGGREGATE Σpnl / Σmfe over trades that had favorable
    excursion — NOT the mean of per-trade p/m ratios. The mean-of-ratios form
    was unstable: a single trade with a small MFE and a loss (e.g. pnl -5.10%
    on mfe +0.86% → ratio -593%) swamped the average and produced a nonsense
    "MFE capture -85%". The aggregate is bounded by the favorable move actually
    available and is the figure both the live and sim panels should agree on.
    """
    if not trades:
        return {
            "trade_count": 0, "win_count": 0, "win_rate": 0.0,
            "session_pnl": 0.0, "avg_mfe": 0.0, "capture_ratio": 0.0,
            "avg_premium": 0.0,
        }
    pnls = [t["pnl_pct"] for t in trades]
    mfes = [t.get("mfe_pct") or 0.0 for t in trades]
    wins = [p for p in pnls if p > 0]
    cap_num = sum(p for p, m in zip(pnls, mfes) if m > 0)
    cap_den = sum(m for m in mfes if m > 0)
    return {
        "trade_count": len(trades),
        "win_count": len(wins),
        "win_rate": len(wins) / len(trades),
        "session_pnl": sum(pnls),
        "avg_mfe": sum(mfes) / len(mfes),
        "capture_ratio": (cap_num / cap_den) if cap_den > 0 else 0.0,
        "avg_premium": sum(t["prem_in"] for t in trades) / len(trades),
    }


def _run_sim_thread(job_id: str, trade_date: str, overrides: dict[str, str]) -> None:
    """Run today's sim in a background thread. Updates _jobs[job_id] in place."""
    with _jobs_lock:
        _jobs[job_id]["status"] = "loading"

    try:
        # Baseline = strategy_app's REAL live config from ops_env.json (shared .run
        # volume). The dashboard process env does NOT carry strategy_app's vars, so
        # reading os.getenv here would silently diverge from live (e.g. capping the
        # day at RISK_MAX_SESSION_TRADES=6 instead of the live 12). ops_env.json is
        # the source of truth; os.getenv is only a fallback.
        ops_path = STRATEGY_RUN_DIR / "ops_env.json"
        live: dict[str, str] = {}
        if ops_path.exists():
            try:
                live = json.loads(ops_path.read_text(encoding="utf-8"))
            except Exception:
                live = {}

        def _live(key: str, fallback: str) -> str:
            v = live.get(key)
            if v not in (None, ""):
                return str(v)
            env_v = os.getenv(key)
            if env_v not in (None, ""):
                return str(env_v)
            return fallback

        sim_env = {
            # ML model paths + thresholds — from live config
            "ENTRY_ML_MODEL_PATH":     _live("ENTRY_ML_MODEL_PATH",
                          "/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib"),
            "DIRECTION_ML_MODEL_PATH": _live("DIRECTION_ML_MODEL_PATH",
                          "/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib"),
            "ENTRY_ML_MIN_PROB":       _live("ENTRY_ML_MIN_PROB", "0.65"),
            "DIRECTION_ML_WEIGHT":     _live("DIRECTION_ML_WEIGHT", "0.40"),
            "DIRECTION_ML_FILTER_MIN_PROB": _live("DIRECTION_ML_FILTER_MIN_PROB", ""),
            "OPTION_PNL_MODEL_BUNDLE": _live("OPTION_PNL_MODEL_BUNDLE", ""),
            # Disable side effects for sim
            "STRATEGY_REDIS_PUBLISH_ENABLED": "0",
            "MARKET_SESSION_ENABLED":          "0",
            "BRAIN_ENABLED":                   "false",
            "STRATEGY_STARTUP_WARMUP_EVENTS":  "0",
            # Exit + entry + strike config — from live config
            "EXIT_POLICY_STACK_ENABLED":       _live("EXIT_POLICY_STACK_ENABLED", "1"),
            "EXIT_PREMIUM_TARGET_PCT":         _live("EXIT_PREMIUM_TARGET_PCT", "0.04"),
            "EXIT_TRAILING_ACTIVATION_PCT":    _live("EXIT_TRAILING_ACTIVATION_PCT", "0.01"),
            "EXIT_TRAILING_TRAIL_PCT":         _live("EXIT_TRAILING_TRAIL_PCT", "0.005"),
            "EXIT_THESIS_FAIL_BARS":           _live("EXIT_THESIS_FAIL_BARS", "3"),
            "EXIT_THESIS_FAIL_MIN_MFE":        _live("EXIT_THESIS_FAIL_MIN_MFE", "0.002"),
            "CONSENSUS_BYPASS_MIN_CONFIDENCE": _live("CONSENSUS_BYPASS_MIN_CONFIDENCE", "0.65"),
            "DIRECTION_MIN_MARGIN_SIDEWAYS":   _live("DIRECTION_MIN_MARGIN_SIDEWAYS", "2.0"),
            "STRATEGY_STRIKE_SELECTION_POLICY": _live("STRATEGY_STRIKE_SELECTION_POLICY", "smart_strike"),
            "STRATEGY_SMART_STRIKE_ENABLED":   _live("STRATEGY_SMART_STRIKE_ENABLED", "1"),
            "SMART_STRIKE_MIN_PREMIUM":        _live("SMART_STRIKE_MIN_PREMIUM", "600"),
            "SMART_STRIKE_MAX_PREMIUM":        _live("SMART_STRIKE_MAX_PREMIUM", "1300"),
            "STRATEGY_STRIKE_MAX_OTM_STEPS":   _live("STRATEGY_STRIKE_MAX_OTM_STEPS", "8"),
            "STRATEGY_SMART_STRIKE_ENABLED":   _live("STRATEGY_SMART_STRIKE_ENABLED", "1"),
            "SMART_STRIKE_OTM_CONFIDENCE":     _live("SMART_STRIKE_OTM_CONFIDENCE", "0.55"),
            "SMART_STRIKE_OTM2_ENABLED":       _live("SMART_STRIKE_OTM2_ENABLED", "1"),
            "SMART_STRIKE_OTM2_CONFIDENCE":    _live("SMART_STRIKE_OTM2_CONFIDENCE", "0.65"),
            "SMART_STRIKE_OTM3_ENABLED":       _live("SMART_STRIKE_OTM3_ENABLED", "1"),
            "SMART_STRIKE_OTM3_CONFIDENCE":    _live("SMART_STRIKE_OTM3_CONFIDENCE", "0.75"),
            "SMART_STRIKE_OTM3_REGIMES":       _live("SMART_STRIKE_OTM3_REGIMES", "BREAKOUT,TRENDING"),
            "SMART_STRIKE_OTM4_ENABLED":       _live("SMART_STRIKE_OTM4_ENABLED", "1"),
            "SMART_STRIKE_OTM4_CONFIDENCE":    _live("SMART_STRIKE_OTM4_CONFIDENCE", "0.85"),
            "SMART_STRIKE_OTM4_REGIMES":       _live("SMART_STRIKE_OTM4_REGIMES", "BREAKOUT"),
            # OI minimums — pass live values; lower defaults so OTM tiers are reachable
            "SMART_STRIKE_OTM2_MIN_OI":         _live("SMART_STRIKE_OTM2_MIN_OI", "20000"),
            "SMART_STRIKE_OTM3_MIN_OI":         _live("SMART_STRIKE_OTM3_MIN_OI", "15000"),
            "SMART_STRIKE_OTM4_MIN_OI":         _live("SMART_STRIKE_OTM4_MIN_OI", "10000"),
            # IV ceilings as PERCENTILE thresholds (moderate experiment). Live still
            # pins the old absolute-style 60/50/40/30 via env; the sim uses corrected
            # percentile ceilings so OTM is reachable in normal IV. Promote to live by
            # setting these in .env.compose once validated here.
            "SMART_STRIKE_OTM_IV_CEIL":        _live("SMART_STRIKE_OTM_IV_CEIL_SIM", "92"),
            "SMART_STRIKE_OTM2_IV_CEIL":       _live("SMART_STRIKE_OTM2_IV_CEIL_SIM", "91"),
            "SMART_STRIKE_OTM3_IV_CEIL":       _live("SMART_STRIKE_OTM3_IV_CEIL_SIM", "90"),
            "SMART_STRIKE_OTM4_IV_CEIL":       _live("SMART_STRIKE_OTM4_IV_CEIL_SIM", "89"),
            "STRATEGY_ENHANCED_VELOCITY":      _live("STRATEGY_ENHANCED_VELOCITY", "0"),
            "STRATEGY_IV_EXTREME_PERCENTILE":  _live("STRATEGY_IV_EXTREME_PERCENTILE", "95.0"),
            "STRATEGY_PROFILE_ID":             _live("STRATEGY_PROFILE_ID",
                                                     "trader_master_ml_entry_consensus_v1"),
            # Risk limits — mirror live exactly (live currently runs 20 session trades)
            "RISK_MAX_CONSECUTIVE_LOSSES":     _live("RISK_MAX_CONSECUTIVE_LOSSES", "3"),
            "RISK_MAX_SESSION_TRADES":         _live("RISK_MAX_SESSION_TRADES", "20"),
            "RISK_MAX_LOTS_PER_TRADE":         _live("RISK_MAX_LOTS_PER_TRADE", "5"),
            "RISK_CAPITAL_ALLOCATED":          _live("RISK_CAPITAL_ALLOCATED", "500000"),
            "RISK_PER_TRADE_PCT":              _live("RISK_PER_TRADE_PCT", "0.005"),
            "STRATEGY_MIN_CONFIDENCE":         _live("STRATEGY_MIN_CONFIDENCE", "") or _live("CONSENSUS_BYPASS_MIN_CONFIDENCE", "0.80") or "0.80",
            # Exit strategy mode — adaptive (live default): scalper for SIDEWAYS/CHOP,
            # lottery runner for BREAKOUT/TRENDING. Lottery params ATM-tuned.
            "EXIT_STRATEGY_MODE":              _live("EXIT_STRATEGY_MODE", "adaptive"),
            "LOTTERY_HARD_STOP_PCT":           _live("LOTTERY_HARD_STOP_PCT", "0.15"),
            "LOTTERY_BIG_TARGET_PCT":          _live("LOTTERY_BIG_TARGET_PCT", "0.40"),
            "LOTTERY_RUNNER_ACTIVATION_MFE":   _live("LOTTERY_RUNNER_ACTIVATION_MFE", "0.15"),
            "LOTTERY_RUNNER_GIVEBACK_FRAC":    _live("LOTTERY_RUNNER_GIVEBACK_FRAC", "0.30"),
            "LOTTERY_THESIS_FAIL_BARS":        _live("LOTTERY_THESIS_FAIL_BARS", "5"),
            "LOTTERY_THESIS_FAIL_MIN_MFE":     _live("LOTTERY_THESIS_FAIL_MIN_MFE", "0.03"),
            "LOTTERY_TIMESTOP_BARS":           _live("LOTTERY_TIMESTOP_BARS", "60"),
            "LOTTERY_MOMENTUM_FLIP":           _live("LOTTERY_MOMENTUM_FLIP", "1.0"),
            # v2 gate-cascade pipeline — off by default, togglable from OPS panel
            "STRATEGY_ENTRY_PIPELINE_V2":      _live("STRATEGY_ENTRY_PIPELINE_V2", "0"),
            "SMART_STRIKE_MIN_PREMIUM":        _live("SMART_STRIKE_MIN_PREMIUM", "0"),
            "STRATEGY_RUN_DIR":                f"/tmp/sim_{job_id}",
            "REDIS_HOST":                      os.getenv("REDIS_HOST", "localhost"),
            "DEPTH_FEED_ENABLED":              "0",
        }
        # Apply user overrides (validated keys only) — these are the deltas the
        # operator dialed in the OPS panel, layered on top of the live baseline.
        for k, v in overrides.items():
            if k in _SAFE_OVERRIDE_KEYS:
                sim_env[k] = str(v)

        # CONSENSUS_BYPASS_MIN_CONFIDENCE is the UI-visible "min confidence" slider.
        # The engine ALSO reads STRATEGY_MIN_CONFIDENCE as the primary entry gate
        # (passed to DeterministicRuleEngine.__init__ min_confidence param).
        # Mirror the user's confidence override to both so either key controls entries.
        if "CONSENSUS_BYPASS_MIN_CONFIDENCE" in overrides:
            sim_env["STRATEGY_MIN_CONFIDENCE"] = sim_env["CONSENSUS_BYPASS_MIN_CONFIDENCE"]
        if "STRATEGY_MIN_CONFIDENCE" in overrides:
            sim_env["CONSENSUS_BYPASS_MIN_CONFIDENCE"] = sim_env["STRATEGY_MIN_CONFIDENCE"]

        # Load snapshots — kept in memory for Mongo persist after run
        snaps = _load_today_snapshots(trade_date)
        total = len(snaps)
        with _jobs_lock:
            _jobs[job_id]["total"] = total
            _jobs[job_id]["status"] = "running"

        if total == 0:
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = f"No snapshots found for {trade_date}"
            return

        Path(f"/tmp/sim_{job_id}").mkdir(exist_ok=True)

        # Apply env + run engine under a lock so env changes don't bleed
        with _ENV_LOCK:
            old_env = {}
            for k, v in sim_env.items():
                old_env[k] = os.environ.get(k)
                os.environ[k] = v
            try:
                trades, exit_stack_name, decision_traces = _run_engine(snaps, trade_date, job_id)
            finally:
                for k, old in old_env.items():
                    if old is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = old

        # Build summary (aggregate capture ratio — see _summarize_trades)
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "done",
                "trades": trades,
                "exit_stack": exit_stack_name,
                "summary": _summarize_trades(trades),
                "overrides_applied": {k: v for k, v in overrides.items() if k in _SAFE_OVERRIDE_KEYS},
            })

        # Persist to Mongo sim collections so the Replay terminal can show it.
        # NOTE: snapshots/positions are persisted under "ops-sim-{job_id}" — the
        # SAME id the replay UI queries. _run_engine set job["run_id"] to the
        # engine-internal "sim-{job_id}" which has NO mongo data; overwrite it
        # here so any UI consumer of job.run_id points at the persisted run.
        try:
            replay_url = _persist_sim_to_mongo(job_id, trade_date, snaps, trades, decision_traces)
            with _jobs_lock:
                _jobs[job_id]["replay_url"] = replay_url
                _jobs[job_id]["run_id"] = f"ops-sim-{job_id}"
        except Exception as exc:
            logger.warning("ops sim: failed to persist to mongo (non-fatal): %s", exc)

    except Exception as exc:
        import traceback
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)
            _jobs[job_id]["traceback"] = traceback.format_exc()


_IST = timezone(timedelta(hours=5, minutes=30))


def _persist_sim_to_mongo(
    job_id: str,
    trade_date: str,
    snaps: list[dict],
    trades: list[dict],
    decision_traces: Optional[list[dict]] = None,
) -> str:
    """Write snapshots + position events to sim Mongo collections, create eval-run entry.

    Returns the replay_url the UI can navigate to (/app?mode=replay&kind=sim&...).
    Non-fatal — caller should swallow exceptions and log rather than failing the job.
    """
    import sys
    from pymongo import MongoClient

    repo = Path("/app")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from contracts_app import resolve_namespace

    run_id = f"ops-sim-{job_id}"
    ns = resolve_namespace("sim", run_id=run_id)
    coll_snaps = ns.collection_for("phase1_market_snapshots")
    coll_pos = ns.collection_for("strategy_positions")
    coll_traces = ns.collection_for("strategy_decision_traces")
    runs_coll = str(os.getenv("MONGO_COLL_STRATEGY_EVAL_RUNS") or "strategy_eval_runs")

    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        client = MongoClient(uri)
    else:
        client = MongoClient(
            host=str(os.getenv("MONGO_HOST") or "localhost"),
            port=int(os.getenv("MONGO_PORT") or "27017"),
        )
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    db = client[db_name]

    # ── Snapshots → phase1_market_snapshots_sim ──────────────────────────────
    def _parse_ts(raw: Any) -> datetime:
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str):
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(raw[:25], fmt)
                except ValueError:
                    pass
            try:
                # Strip timezone suffix and treat as IST
                return datetime.fromisoformat(raw[:19]).replace(tzinfo=_IST)
            except ValueError:
                pass
        return datetime.now(tz=_IST)

    snap_docs = []
    for snap in snaps:
        fb = snap.get("futures_bar") or {}
        sc = snap.get("session_context") or {}
        ts = _parse_ts(snap.get("timestamp"))
        snap_docs.append({
            "run_id": run_id,
            "trade_date_ist": trade_date,
            "timestamp": ts,
            "instrument": snap.get("instrument", "BANKNIFTY-I"),
            "payload": {
                "snapshot": {
                    "futures_bar": {
                        "fut_open":   float(fb.get("open") or fb.get("o") or fb.get("fut_open") or 0),
                        "fut_high":   float(fb.get("high") or fb.get("h") or fb.get("fut_high") or 0),
                        "fut_low":    float(fb.get("low") or fb.get("l") or fb.get("fut_low") or 0),
                        "fut_close":  float(fb.get("close") or fb.get("c") or fb.get("fut_close") or 0),
                        "fut_volume": int(float(fb.get("volume") or fb.get("v") or fb.get("fut_volume") or 0)),
                    },
                    "session_context": sc,
                }
            },
        })
    if snap_docs:
        db[coll_snaps].insert_many(snap_docs)

    # ── Trades → POSITION_OPEN + POSITION_CLOSE events ───────────────────────
    def _hhmm_to_dt(hhmm: str) -> datetime:
        try:
            h, m = int(hhmm[:2]), int(hhmm[3:5])
            y, mo, d = [int(p) for p in trade_date.split("-")]
            return datetime(y, mo, d, h, m, 0, tzinfo=_IST)
        except Exception:
            return datetime.now(tz=_IST)

    pos_docs = []
    for i, t in enumerate(trades):
        pid = f"{run_id}-pos-{i}"
        open_ts = _hhmm_to_dt(str(t.get("time_in", "09:15")))
        close_ts = _hhmm_to_dt(str(t.get("time_out", "15:25")))
        common = {"direction": t.get("direction", ""), "strike": t.get("strike")}
        pos_docs.append({
            "event": "POSITION_OPEN",
            "position_id": pid,
            "signal_id": pid,
            "timestamp": open_ts,
            "trade_date_ist": trade_date,
            "run_id": run_id,
            "payload": {"position": {
                "entry_premium":      float(t.get("prem_in") or 0),
                "entry_strategy":     str(t.get("strategy_name") or ""),
                "entry_strategy_name":str(t.get("strategy_name") or ""),
                "strategy":           str(t.get("strategy_name") or ""),
                "reason":             str(t.get("entry_reason") or ""),
                **common,
            }},
        })
        pos_docs.append({
            "event": "POSITION_CLOSE",
            "position_id": pid,
            "signal_id": pid,
            "timestamp": close_ts,
            "trade_date_ist": trade_date,
            "run_id": run_id,
            "payload": {
                "position": {
                    "entry_premium": float(t.get("prem_in") or 0),
                    "exit_premium":  float(t.get("prem_out") or 0),
                    "pnl_pct":       float(t.get("pnl_pct") or 0),
                    "mfe_pct":       float(t.get("mfe_pct") or 0),
                    "mae_pct":       float(t.get("mae_pct") or 0),
                    "exit_reason":   str(t.get("exit") or ""),
                    **common,
                }
            },
        })
    if pos_docs:
        db[coll_pos].insert_many(pos_docs)

    # ── v2 gate-cascade decision traces → Terminal decision view ─────────────
    # Each element is the last_entry_trace dict from the engine: decision_id,
    # timestamp, final_outcome, primary_blocker_gate, gates[]. The Terminal's
    # existing decision-trace view reads this collection and shows
    # ENTER/SKIP/VETO per bar with the gate that stopped it.
    if decision_traces:
        trace_docs = []
        for tr in decision_traces:
            ts_raw = tr.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now(tz=_IST)
            except Exception:
                ts = datetime.now(tz=_IST)
            trace_docs.append({
                "run_id": run_id,
                "trade_date_ist": trade_date,
                "timestamp": ts,
                "decision_id": tr.get("decision_id"),
                "snapshot_id": tr.get("snapshot_id"),
                "final_outcome": tr.get("final_outcome"),
                "primary_blocker_gate": tr.get("primary_blocker_gate"),
                "selected_direction": tr.get("selected_direction"),
                "selected_strike": tr.get("selected_strike"),
                "selected_premium": tr.get("selected_premium"),
                "gates": tr.get("gates", []),
                # Terminal-compatible fields (mirrors v1 trace shape)
                "engine_mode": "v2_gate_cascade",
                "decision_mode": "v2",
                "evaluation_type": "entry",
                "flow_gates": [
                    {
                        "gate_id": g["gate"],
                        "gate_group": g["gate"],
                        "status": "pass" if g["outcome"] == "pass" else "blocked",
                        "reason_code": g.get("reason") or None,
                        "metrics": {k: float(v) for k, v in (g.get("values") or {}).items()
                                    if isinstance(v, (int, float))},
                    }
                    for g in tr.get("gates", [])
                ],
                "candidates": [],
            })
        if trace_docs:
            db[coll_traces].insert_many(trace_docs)

    # ── strategy_eval_runs entry ─────────────────────────────────────────────
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    try:
        db[runs_coll].insert_one({
            "run_id": run_id,
            "kind": "sim",
            "status": "completed",
            "terminal_status": "completed",
            "source_date": trade_date,
            "source_coll": "ops_sim",
            "label": f"ops-sim {trade_date}",
            "speed": 0.0,
            "env_overrides": {},
            "submitted_at": now_iso,
            "updated_at": now_iso,
        })
    except Exception:
        pass  # duplicate key if user re-runs same job; not fatal

    return f"/app?mode=replay&kind=sim&run_id={run_id}&date={trade_date}"


def _run_engine(snaps: list[dict], trade_date: str, job_id: str) -> tuple[list[dict], str, list[dict]]:
    """Run the deterministic engine over today's snapshots. Returns (trades, exit_stack_name).

    Delegates to strategy_app.sim.replay_engine.replay_day() — the shared implementation
    used by both the OPS same-day sim and the multi-day sim. Config is already set in
    os.environ by the caller (_run_sim_thread). Progress is forwarded via callback.
    """
    import sys
    repo = Path("/app")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    from strategy_app.sim.replay_engine import replay_day

    run_id = f"sim-{job_id}"
    os.environ.setdefault("STRATEGY_RUN_ID", run_id)
    with _jobs_lock:
        _jobs[job_id]["run_id"] = run_id

    def _progress(i: int, total: int) -> None:
        with _jobs_lock:
            _jobs[job_id]["progress"] = i

    result = replay_day(snaps, trade_date, progress_cb=_progress)

    with _jobs_lock:
        _jobs[job_id]["progress"] = len(snaps)
        _jobs[job_id]["diag"] = result["diag"]

    return result["trades"], result["exit_stack_name"], result.get("decision_traces", [])


# ── Router ─────────────────────────────────────────────────────────────────────

class OpsRouter:
    def __init__(self) -> None:
        router = APIRouter(prefix="/api/ops", tags=["ops"])
        router.add_api_route("/config",           self.get_config,    methods=["GET"])
        router.add_api_route("/sim/today",        self.post_sim_today, methods=["POST"])
        router.add_api_route("/sim/{job_id}",     self.get_sim_status, methods=["GET"])
        self.router = router

    async def get_config(self):
        return _read_live_config()

    async def post_sim_today(self, req: SimTodayRequest):
        trade_date = req.date or date.today().isoformat()

        # Validate overrides
        bad_keys = set(req.overrides.keys()) - _SAFE_OVERRIDE_KEYS
        if bad_keys:
            raise HTTPException(400, f"Disallowed override keys: {bad_keys}")

        job_id = str(uuid.uuid4())[:12]
        with _jobs_lock:
            _jobs[job_id] = {
                "job_id":    job_id,
                "date":      trade_date,
                "status":    "queued",
                "progress":  0,
                "total":     0,
                "trades":    [],
                "summary":   {},
                "overrides": req.overrides,
                "created_at": datetime.now().isoformat(),
            }

        # Load actual trades immediately so UI can show them while sim runs
        actual = _load_actual_trades(trade_date)
        with _jobs_lock:
            _jobs[job_id]["actual_trades"] = actual

        t = threading.Thread(
            target=_run_sim_thread,
            args=(job_id, trade_date, req.overrides),
            daemon=True,
            name=f"sim-{job_id}",
        )
        t.start()

        return {"job_id": job_id, "date": trade_date, "actual_trade_count": len(actual)}

    async def get_sim_status(self, job_id: str):
        with _jobs_lock:
            job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"Job {job_id} not found")
        return dict(job)
