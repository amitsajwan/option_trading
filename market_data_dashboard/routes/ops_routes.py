"""OPS tab backend — daily sim runner + live config reader.

Endpoints:
    GET  /api/ops/config          current live config (from runtime_config.json + env)
    POST /api/ops/sim/today       start today's sim with optional overrides → {job_id}
    GET  /api/ops/sim/{job_id}    poll progress + results
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
    "DIRECTION_MIN_MARGIN_SIDEWAYS",
    "STRATEGY_STRIKE_SELECTION_POLICY",
    "SMART_STRIKE_MAX_PREMIUM",
    "STRATEGY_STRIKE_MAX_OTM_STEPS",
    "RISK_MAX_CONSECUTIVE_LOSSES",
    "RISK_MAX_SESSION_TRADES",
    "STRATEGY_PROFILE_ID",
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
        "SMART_STRIKE_MAX_PREMIUM":       _e("SMART_STRIKE_MAX_PREMIUM", "600"),
        "STRATEGY_STRIKE_MAX_OTM_STEPS":  _e("STRATEGY_STRIKE_MAX_OTM_STEPS", "0"),
        "RISK_MAX_CONSECUTIVE_LOSSES":    _e("RISK_MAX_CONSECUTIVE_LOSSES", "3"),
        "RISK_MAX_SESSION_TRADES":        _e("RISK_MAX_SESSION_TRADES", "6"),
        "STRATEGY_PROFILE_ID":            _e("STRATEGY_PROFILE_ID", "trader_master_ml_entry_consensus_v1"),
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

def _load_actual_trades(trade_date: str) -> list[dict]:
    pos_path = STRATEGY_RUN_DIR / "positions.jsonl"
    if not pos_path.exists():
        return []
    positions: dict[str, dict] = {}
    for line in pos_path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            pid = d.get("position_id", "")
            evt = d.get("event", "")
            if evt in ("POSITION_OPEN", "POSITION_MANAGE", "POSITION_CLOSE"):
                if evt == "POSITION_CLOSE" or pid not in positions:
                    positions[pid] = d
        except Exception:
            pass
    trades = []
    for p in positions.values():
        if p.get("event") == "POSITION_CLOSE":
            ts = str(p.get("timestamp", ""))
            exit_r = p.get("exit_reason", "")
            label = str(p.get("exit_policy_triggered") or exit_r or "")
            trades.append({
                "time_in":  ts[11:16] if len(ts) > 15 else "?",
                "time_out": ts[11:16] if len(ts) > 15 else "?",
                "direction": p.get("direction", ""),
                "strike":   p.get("strike"),
                "prem_in":  float(p.get("entry_premium") or 0),
                "prem_out": float(p.get("exit_premium") or p.get("entry_premium") or 0),
                "pnl_pct":  float(p.get("pnl_pct") or 0),
                "mfe_pct":  float(p.get("mfe_pct") or 0),
                "mae_pct":  float(p.get("mae_pct") or 0),
                "bars":     int(p.get("bars_held") or 0),
                "exit":     label,
                "source":   "actual",
            })
    trades.sort(key=lambda x: x["time_in"])
    return trades


# ── Sim runner ─────────────────────────────────────────────────────────────────

_ENV_LOCK = threading.Lock()


def _run_sim_thread(job_id: str, trade_date: str, overrides: dict[str, str]) -> None:
    """Run today's sim in a background thread. Updates _jobs[job_id] in place."""
    with _jobs_lock:
        _jobs[job_id]["status"] = "loading"

    try:
        # Build the env for this sim — base env + ML paths + overrides
        sim_env = {
            # ML model paths from live container
            "ENTRY_ML_MODEL_PATH":
                os.getenv("ENTRY_ML_MODEL_PATH",
                          "/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib"),
            "DIRECTION_ML_MODEL_PATH":
                os.getenv("DIRECTION_ML_MODEL_PATH",
                          "/app/ml_pipeline_2/artifacts/direction_only/published/direction_only_model.joblib"),
            "ENTRY_ML_MIN_PROB":      os.getenv("ENTRY_ML_MIN_PROB", "0.65"),
            "DIRECTION_ML_WEIGHT":    os.getenv("DIRECTION_ML_WEIGHT", "0.40"),
            "OPTION_PNL_MODEL_BUNDLE": os.getenv("OPTION_PNL_MODEL_BUNDLE", ""),
            # Disable side effects for sim
            "STRATEGY_REDIS_PUBLISH_ENABLED": "0",
            "MARKET_SESSION_ENABLED":          "0",
            "BRAIN_ENABLED":                   "false",
            "STRATEGY_STARTUP_WARMUP_EVENTS":  "0",
            # Current live config as base
            "EXIT_POLICY_STACK_ENABLED":       os.getenv("EXIT_POLICY_STACK_ENABLED", "1"),
            "EXIT_PREMIUM_TARGET_PCT":         os.getenv("EXIT_PREMIUM_TARGET_PCT", "0.04"),
            "EXIT_TRAILING_ACTIVATION_PCT":    os.getenv("EXIT_TRAILING_ACTIVATION_PCT", "0.01"),
            "EXIT_TRAILING_TRAIL_PCT":         os.getenv("EXIT_TRAILING_TRAIL_PCT", "0.005"),
            "EXIT_THESIS_FAIL_BARS":           os.getenv("EXIT_THESIS_FAIL_BARS", "3"),
            "EXIT_THESIS_FAIL_MIN_MFE":        os.getenv("EXIT_THESIS_FAIL_MIN_MFE", "0.002"),
            "CONSENSUS_BYPASS_MIN_CONFIDENCE": os.getenv("CONSENSUS_BYPASS_MIN_CONFIDENCE", "0.65"),
            "DIRECTION_MIN_MARGIN_SIDEWAYS":   os.getenv("DIRECTION_MIN_MARGIN_SIDEWAYS", "2.0"),
            "STRATEGY_STRIKE_SELECTION_POLICY": os.getenv("STRATEGY_STRIKE_SELECTION_POLICY", "smart_strike"),
            "SMART_STRIKE_MAX_PREMIUM":        os.getenv("SMART_STRIKE_MAX_PREMIUM", "800"),
            "STRATEGY_STRIKE_MAX_OTM_STEPS":   os.getenv("STRATEGY_STRIKE_MAX_OTM_STEPS", "8"),
            "STRATEGY_SMART_STRIKE_ENABLED":   os.getenv("STRATEGY_SMART_STRIKE_ENABLED", "1"),
            "SMART_STRIKE_OTM_CONFIDENCE":     os.getenv("SMART_STRIKE_OTM_CONFIDENCE", "0.55"),
            "SMART_STRIKE_OTM2_ENABLED":       os.getenv("SMART_STRIKE_OTM2_ENABLED", "1"),
            "SMART_STRIKE_OTM2_CONFIDENCE":    os.getenv("SMART_STRIKE_OTM2_CONFIDENCE", "0.65"),
            "SMART_STRIKE_OTM3_ENABLED":       os.getenv("SMART_STRIKE_OTM3_ENABLED", "1"),
            "SMART_STRIKE_OTM3_CONFIDENCE":    os.getenv("SMART_STRIKE_OTM3_CONFIDENCE", "0.75"),
            "SMART_STRIKE_OTM3_REGIMES":       os.getenv("SMART_STRIKE_OTM3_REGIMES", "BREAKOUT,TRENDING"),
            "SMART_STRIKE_OTM4_ENABLED":       os.getenv("SMART_STRIKE_OTM4_ENABLED", "1"),
            "SMART_STRIKE_OTM4_CONFIDENCE":    os.getenv("SMART_STRIKE_OTM4_CONFIDENCE", "0.85"),
            "SMART_STRIKE_OTM4_REGIMES":       os.getenv("SMART_STRIKE_OTM4_REGIMES", "BREAKOUT"),
            "STRATEGY_PROFILE_ID":             os.getenv("STRATEGY_PROFILE_ID",
                                                         "trader_master_ml_entry_consensus_v1"),
            "RISK_MAX_CONSECUTIVE_LOSSES":     os.getenv("RISK_MAX_CONSECUTIVE_LOSSES", "3"),
            "RISK_MAX_SESSION_TRADES":         os.getenv("RISK_MAX_SESSION_TRADES", "6"),
            "RISK_CAPITAL_ALLOCATED":          os.getenv("RISK_CAPITAL_ALLOCATED", "500000"),
            "STRATEGY_RUN_DIR":                f"/tmp/sim_{job_id}",
            "REDIS_HOST":                      os.getenv("REDIS_HOST", "localhost"),
            "DEPTH_FEED_ENABLED":              "0",
        }
        # Apply user overrides (validated keys only)
        for k, v in overrides.items():
            if k in _SAFE_OVERRIDE_KEYS:
                sim_env[k] = str(v)

        # Load snapshots
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
                trades, exit_stack_name = _run_engine(snaps, trade_date, job_id)
            finally:
                for k, old in old_env.items():
                    if old is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = old

        # Build summary
        pnls = [t["pnl_pct"] for t in trades]
        mfes = [t["mfe_pct"] for t in trades]
        wins = [p for p in pnls if p > 0]
        avg_prem = sum(t["prem_in"] for t in trades) / len(trades) if trades else 0
        caps = [p / m for p, m in zip(pnls, mfes) if m > 0]
        avg_cap = sum(caps) / len(caps) if caps else 0

        with _jobs_lock:
            _jobs[job_id].update({
                "status": "done",
                "trades": trades,
                "exit_stack": exit_stack_name,
                "summary": {
                    "trade_count": len(trades),
                    "win_count": len(wins),
                    "win_rate": len(wins) / len(trades) if trades else 0,
                    "session_pnl": sum(pnls),
                    "avg_mfe": sum(mfes) / len(mfes) if mfes else 0,
                    "capture_ratio": avg_cap,
                    "avg_premium": avg_prem,
                },
                "overrides_applied": {k: v for k, v in overrides.items() if k in _SAFE_OVERRIDE_KEYS},
            })

    except Exception as exc:
        import traceback
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)
            _jobs[job_id]["traceback"] = traceback.format_exc()


def _run_engine(snaps: list[dict], trade_date: str, job_id: str) -> tuple[list[dict], str]:
    """Run the deterministic engine over today's snapshots. Returns (trades, exit_stack_name)."""
    import sys
    repo = Path("/app")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    from strategy_app.engines import DeterministicRuleEngine
    from strategy_app.engines.profiles import build_run_metadata
    from strategy_app.contracts import SignalType
    from strategy_app.position.exit_policy import build_default_exit_stack

    profile_id = os.getenv("STRATEGY_PROFILE_ID", "trader_master_ml_entry_consensus_v1")
    engine = DeterministicRuleEngine(
        min_confidence=float(os.getenv("STRATEGY_MIN_CONFIDENCE", "0.50")),
        strategy_profile_id=profile_id,
    )
    exit_stack_name = build_default_exit_stack().name

    run_meta = build_run_metadata(profile_id)
    run_meta["risk_config"] = {
        "rollout_stage": "paper",
        "position_size_multiplier": 1.0,
        "halt_consecutive_losses": int(os.getenv("RISK_MAX_CONSECUTIVE_LOSSES", "3")),
        "halt_daily_dd_pct": 0.04,
    }
    engine.set_run_context(f"sim-{job_id}", run_meta)

    trade_date_obj = date.fromisoformat(trade_date)
    engine.on_session_start(trade_date_obj)

    trades = []
    current_entry: Optional[dict] = None
    total = len(snaps)

    for i, snap in enumerate(snaps):
        # Update progress every 20 snapshots
        if i % 20 == 0:
            with _jobs_lock:
                _jobs[job_id]["progress"] = i

        try:
            signal = engine.evaluate(snap)
        except Exception:
            continue

        if signal is None:
            continue

        ts = str(snap.get("timestamp", ""))
        hhmm = ts[11:16] if len(ts) > 15 else "?"

        if signal.signal_type == SignalType.ENTRY:
            current_entry = {
                "time_in": hhmm,
                "direction": signal.direction,
                "strike": signal.strike,
                "prem_in": float(signal.entry_premium or 0),
                "lots": signal.max_lots,
            }

        elif signal.signal_type == SignalType.EXIT and current_entry is not None:
            closed = engine._tracker._closed_positions
            if closed:
                cp = closed[-1]
                pnl_pct = float(cp.get("pnl_pct", 0))
                mfe_pct  = float(cp.get("mfe_pct", 0))
                mae_pct  = float(cp.get("mae_pct", 0))
                exit_prem = float(cp.get("exit_premium", current_entry["prem_in"]))
                exit_trigger = str(cp.get("exit_policy_triggered") or "")
                exit_reason  = str(cp.get("exit_reason") or "")
                label = exit_trigger if exit_trigger else exit_reason
            else:
                pnl_pct = mfe_pct = mae_pct = 0.0
                exit_prem = current_entry["prem_in"]
                label = signal.exit_reason.value if signal.exit_reason else "?"

            trades.append({
                "time_in":  current_entry["time_in"],
                "time_out": hhmm,
                "direction": current_entry["direction"],
                "strike":   current_entry["strike"],
                "prem_in":  current_entry["prem_in"],
                "prem_out": exit_prem,
                "pnl_pct":  pnl_pct,
                "mfe_pct":  mfe_pct,
                "mae_pct":  mae_pct,
                "lots":     current_entry["lots"],
                "exit":     label,
                "source":   "sim",
            })
            current_entry = None

    engine.on_session_end(trade_date_obj)
    with _jobs_lock:
        _jobs[job_id]["progress"] = total
    return trades, exit_stack_name


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
