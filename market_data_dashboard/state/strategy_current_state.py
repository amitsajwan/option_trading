"""JSONL-first reader for the *currently-running* strategy_app session.

Backs the `/api/strategy/current/*` endpoints. Reads directly from the
canonical JSONL files written by `strategy_app.logging.signal_logger` and
the `health_marker.json` written when a critical append fails.

Per ARCHITECTURE.md §9 the split-by-query-type rule:
- current run / current session  → JSONL (this module)
- cross-day aggregates           → MongoDB (existing services)

This module intentionally does NO mongo work; pure filesystem reads so that
the endpoint surfaces correct data even when the mongo persistence path is
slow or unavailable.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


DEFAULT_HISTORICAL_RUN_DIR = Path("/app/.run/strategy_app_historical")
DEFAULT_LIVE_RUN_DIR = Path("/app/.run/strategy_app")
DEFAULT_PUBLISHED_MODELS_ROOT = Path("/app/ml_pipeline_2/artifacts/published_models")


@dataclass
class _CurrentState:
    mode: str
    run_id: Optional[str]
    jsonl_path: str
    file_exists: bool
    file_size_bytes: int
    health_marker: dict[str, Any]
    stats: dict[str, Any]
    latest_positions: list[dict[str, Any]]
    # Model + engine the strategy_app self-declared at start. Read from
    # runtime_config.json. Tells the operator WHAT is producing the events.
    runtime_config: dict[str, Any]
    # Other published models available on disk (could be switched to with a
    # container restart). Tells the operator WHAT ELSE could be loaded.
    available_models: list[dict[str, Any]]


def _resolve_run_dir(mode: str) -> Path:
    """Return the on-disk run_dir for the given mode.

    Order of precedence:
      1. Explicit env override (`STRATEGY_RUN_DIR_LIVE` / `STRATEGY_RUN_DIR_HISTORICAL`)
      2. Default by mode
    """
    mode = mode.strip().lower()
    if mode in {"historical", "replay"}:
        return Path(os.getenv("STRATEGY_RUN_DIR_HISTORICAL") or DEFAULT_HISTORICAL_RUN_DIR)
    return Path(os.getenv("STRATEGY_RUN_DIR_LIVE") or DEFAULT_LIVE_RUN_DIR)


def _tail_lines(path: Path, n: int = 50, max_bytes_back: int = 2_000_000) -> list[str]:
    """Read the last `n` lines from a (potentially very large) JSONL file
    efficiently — seek to near-end, scan backwards.

    `max_bytes_back` is a safety cap to avoid pathological I/O on truly
    huge files. For our positions.jsonl files (typically < 50 MB) the
    cap is never reached.
    """
    if not path.exists():
        return []
    size = path.stat().st_size
    if size == 0:
        return []
    read_from = max(0, size - max_bytes_back)
    try:
        with path.open("rb") as f:
            f.seek(read_from)
            tail = f.read()
    except Exception:
        return []
    text = tail.decode("utf-8", errors="replace")
    # If we didn't start at byte 0, the first partial line is incomplete — drop it.
    if read_from > 0 and "\n" in text:
        text = text.split("\n", 1)[1]
    # Use splitlines() so we handle \n, \r\n, and \r consistently.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Yield parsed JSON dicts from a JSONL file. Skips malformed lines."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _read_runtime_config(run_dir: Path) -> dict[str, Any]:
    """Read the strategy_app's self-described runtime_config.json.

    Returns a small dict describing the currently-loaded model + engine
    (the answer to 'what's actually running right now'). Empty dict if
    the file doesn't exist or is malformed — caller treats absence as
    'strategy_app hasn't started yet for this mode'.
    """
    path = run_dir / "runtime_config.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"error": "runtime_config.json unreadable"}
    # Surface a small, stable shape — don't leak the whole config.
    # NOTE: "model" block reflects whatever model is ACTUALLY firing decisions.
    # When the engine runs an option-P&L bundle, runtime_config.model holds the
    # bundle's recipe info (model_type="option_pnl_v1"); the original staged
    # package gets demoted to "model_legacy_staged" for traceability only.
    model = payload.get("model") or {}
    legacy = payload.get("model_legacy_staged") or None
    rollout = payload.get("rollout") or {}
    engine = str(payload.get("engine") or "")
    profile_id = str(payload.get("strategy_profile_id") or "").strip()
    if engine == "deterministic":
        model_type = f"deterministic ({profile_id})" if profile_id else "deterministic_rules"
    elif str(model.get("model_type") or "") == "option_pnl_v1":
        model_type = "option_pnl_v1"
    else:
        model_type = model.get("model_type") or "staged_runtime_v1"
    out = {
        "engine": payload.get("engine"),
        "topic": payload.get("topic"),
        "strategy_profile_id": profile_id or None,
        "model_run_id": model.get("run_id"),
        "model_group": model.get("model_group"),
        "model_package_path": model.get("model_package_path"),
        "model_type": model_type,
        "rollout_stage": rollout.get("stage"),
        "min_confidence": rollout.get("min_confidence"),
        "position_size_multiplier": rollout.get("position_size_multiplier"),
        "halt_consecutive_losses": rollout.get("halt_consecutive_losses"),
        "halt_daily_dd_pct": rollout.get("halt_daily_dd_pct"),
        "block_expiry": model.get("block_expiry"),
        "checked_at_ist": payload.get("checked_at_ist"),
    }
    # Option-P&L bundle extras (only present when bundle is active)
    if str(model.get("model_type") or "") == "option_pnl_v1":
        out["recipe_id"] = model.get("recipe_id")
        out["decision_threshold"] = model.get("decision_threshold")
        out["option_type"] = model.get("option_type")
        out["max_hold_bars"] = model.get("max_hold_bars")
        out["stop_pct_of_premium"] = model.get("stop_pct_of_premium")
        out["target_pct_of_premium"] = model.get("target_pct_of_premium")
        # Multi-bundle: list all loaded models so dashboard shows the full set.
        # Each entry: {recipe_id, option_type, decision_threshold, run_id}
        bundles = model.get("bundles")
        if isinstance(bundles, list) and len(bundles) > 1:
            out["active_bundles"] = bundles
    if legacy:
        # Visible-but-demoted: dashboard can show "fallback model loaded as
        # placeholder" so operator sees the full picture.
        out["legacy_staged_model"] = {
            "run_id": legacy.get("run_id"),
            "model_group": legacy.get("model_group"),
        }
    # Merge key operational knobs from ops_env.json (written by strategy_app at startup).
    ops_path = run_dir / "ops_env.json"
    if ops_path.exists():
        try:
            ops = json.loads(ops_path.read_text(encoding="utf-8"))
            out["exit_strategy_mode"] = str(ops.get("EXIT_STRATEGY_MODE") or "scalper").strip().lower()
            out["exit_stack_name"] = str(ops.get("EXIT_STACK_NAME") or "").strip() or None
            out["risk_max_session_trades"] = _safe_int(ops.get("RISK_MAX_SESSION_TRADES"))
            out["risk_max_consecutive_losses"] = _safe_int(ops.get("RISK_MAX_CONSECUTIVE_LOSSES"))
            out["smart_strike_enabled"] = str(ops.get("STRATEGY_SMART_STRIKE_ENABLED") or "0") == "1"
        except Exception:
            pass
    return out


def _safe_int(v: Any) -> Any:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _list_available_models(root: Optional[Path] = None) -> list[dict[str, Any]]:
    """Scan published_models/.../training_runs/<RUN_ID>/model/model.joblib and
    return one entry per available run_id.

    Output shape per entry:
        { "run_id": "...", "model_group": "...", "model_package_path": "..." }

    Filesystem-only: no network, no mongo, no strategy_app dependency.
    """
    base = root or DEFAULT_PUBLISHED_MODELS_ROOT
    if not base.exists():
        return []
    # Walk: <root>/<group_a>/<group_b>/data/training_runs/<RUN_ID>/model/model.joblib
    entries: list[dict[str, Any]] = []
    for joblib_path in base.glob("*/*/data/training_runs/*/model/model.joblib"):
        try:
            run_id = joblib_path.parent.parent.name
            training_runs_dir = joblib_path.parent.parent.parent
            data_dir = training_runs_dir.parent
            group_root = data_dir.parent
            group_inner = group_root.name
            group_outer = group_root.parent.name
            entries.append({
                "run_id": run_id,
                "model_group": f"{group_outer}/{group_inner}",
                "model_package_path": str(joblib_path),
            })
        except Exception:
            continue
    return sorted(entries, key=lambda e: e["run_id"])


def _read_health_marker(path: Path) -> dict[str, Any]:
    """Read the JSONL health marker file. Missing file = healthy.
    Malformed file = unhealthy (fail-safe), matching HealthMarker.is_healthy."""
    if not path.exists():
        return {"ok": True}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "reason": "marker_unreadable"}


def _compute_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up summary counters across all events in the JSONL."""
    if not records:
        return {
            "total_records": 0,
            "first_event_at": None,
            "last_event_at": None,
            "event_counts": {},
            "run_ids_seen": [],
            "current_run_id": None,
        }
    event_counts: Counter[str] = Counter()
    run_ids: list[str] = []
    first_ts = None
    last_ts = None
    for r in records:
        event_counts[str(r.get("event") or "?")] += 1
        ts = r.get("timestamp") or r.get("entry_time")
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
        run = r.get("run_id")
        if run and (not run_ids or run_ids[-1] != run):
            run_ids.append(run)
    return {
        "total_records": len(records),
        "first_event_at": first_ts,
        "last_event_at": last_ts,
        "event_counts": dict(event_counts),
        "run_ids_seen": run_ids,
        "current_run_id": run_ids[-1] if run_ids else None,
    }


def read_observability_summary(mode: str = "live", *, run_dir: Optional[Path] = None) -> dict[str, Any]:
    """One-stop observability snapshot for the current strategy session.

    Bundles together:
      - Currently-loaded model identity (from runtime_config)
      - Today's gate counts (hold_counts from runtime_state, plus
        derived from signals.jsonl when state is stale)
      - Today's trade count + net P&L from positions.jsonl
      - Last decision summary line (action, blocking_gate, model probs)
      - Health marker

    Designed as the single endpoint a dashboard / cron / alerting layer
    can poll for "is the runtime healthy + producing expected output?"
    No mongo dependency.
    """
    rd = run_dir if run_dir is not None else _resolve_run_dir(mode)
    runtime_state_path = rd / "runtime_state.json"
    decisions_path = rd / "decisions.jsonl"
    positions_path = rd / "positions.jsonl"
    health_marker_path = rd / "health_marker.json"

    # Runtime config (currently loaded model)
    runtime_config = _read_runtime_config(rd)

    # Live runtime_state (engine's own session view — has hold_counts)
    runtime_state: dict[str, Any] = {}
    if runtime_state_path.exists():
        try:
            runtime_state = json.loads(runtime_state_path.read_text(encoding="utf-8")) or {}
        except Exception:
            runtime_state = {}
    session_view = runtime_state.get("session") or {}
    hold_counts = dict(session_view.get("hold_counts") or {})
    bars_evaluated = int(session_view.get("bars_evaluated") or 0)
    trade_date = session_view.get("trade_date")

    # Today's trades from positions.jsonl (last 500 events filtered to
    # POSITION_CLOSE for the active trade_date).
    today_trades = 0
    today_net_pnl = 0.0
    today_wins = 0
    if positions_path.exists() and trade_date:
        date_prefix = str(trade_date).replace("-", "")
        for raw in _tail_lines(positions_path, n=500):
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if rec.get("event") != "POSITION_CLOSE":
                continue
            sid = str(rec.get("snapshot_id") or "")
            if not sid.startswith(date_prefix):
                continue
            today_trades += 1
            pnl = rec.get("pnl_pct")
            try:
                pnl_f = float(pnl) if pnl is not None else 0.0
            except (TypeError, ValueError):
                pnl_f = 0.0
            today_net_pnl += pnl_f
            if pnl_f > 0:
                today_wins += 1

    # Last decision summary (most recent line in decisions.jsonl)
    last_decision: Optional[dict[str, Any]] = None
    if decisions_path.exists():
        for raw in reversed(_tail_lines(decisions_path, n=5)):
            try:
                last_decision = json.loads(raw)
                break
            except Exception:
                continue

    return {
        "mode": mode,
        "run_dir": str(rd),
        "trade_date": trade_date,
        "deployed_model": {
            "engine": runtime_config.get("engine"),
            "model_type": runtime_config.get("model_type"),
            "recipe_id": runtime_config.get("recipe_id"),
            "decision_threshold": runtime_config.get("decision_threshold"),
            "model_run_id": runtime_config.get("model_run_id"),
            "model_package_path": runtime_config.get("model_package_path"),
            "checked_at_ist": runtime_config.get("checked_at_ist"),
        },
        "today": {
            "bars_evaluated": bars_evaluated,
            "hold_counts": hold_counts,
            "trades_closed": today_trades,
            "wins": today_wins,
            "win_rate": (today_wins / today_trades) if today_trades > 0 else None,
            "net_pnl_pct_sum": today_net_pnl,
        },
        "last_decision": last_decision,
        "health": _read_health_marker(health_marker_path),
    }


def read_strategy_current_state(
    mode: str = "live",
    *,
    latest_n: int = 50,
    run_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Build a summary of the currently-running strategy_app session.

    `mode` selects which on-disk run_dir to read. `latest_n` controls how many
    recent position events to include in the response. The call is read-only
    and does NOT touch mongo.
    """
    run_dir_path = Path(run_dir) if run_dir else _resolve_run_dir(mode)
    positions_path = run_dir_path / "positions.jsonl"
    marker_path = run_dir_path / "health_marker.json"

    file_exists = positions_path.exists()
    file_size = positions_path.stat().st_size if file_exists else 0

    # For the stats roll-up we read every record (one full file pass). For most
    # of our positions.jsonl this is fast (<50 MB). For latest_n we use the
    # efficient tail to avoid double-parsing.
    all_records: list[dict[str, Any]] = list(_iter_jsonl(positions_path)) if file_exists else []
    stats = _compute_stats(all_records)

    latest_position_records: list[dict[str, Any]] = []
    if file_exists:
        for raw_line in _tail_lines(positions_path, n=latest_n):
            try:
                latest_position_records.append(json.loads(raw_line))
            except json.JSONDecodeError:
                continue

    health_marker = _read_health_marker(marker_path)
    runtime_config = _read_runtime_config(run_dir_path)
    available_models = _list_available_models()

    state = _CurrentState(
        mode=mode.strip().lower(),
        run_id=stats.get("current_run_id"),
        jsonl_path=str(positions_path),
        file_exists=file_exists,
        file_size_bytes=file_size,
        health_marker=health_marker,
        stats=stats,
        latest_positions=latest_position_records,
        runtime_config=runtime_config,
        available_models=available_models,
    )
    return asdict(state)


def read_blocker_funnel(
    mode: str = "replay",
    *,
    date: str,
    run_dir: Optional[Path] = None,
    top_n_reasons: int = 12,
    top_n_gates: int = 6,
) -> dict[str, Any]:
    """For a given date, scan decision_traces.jsonl and produce a funnel of WHY
    snapshots were blocked from becoming trades.

    Answers the operator question "why no trades on this date?" with concrete
    numbers: how many snapshots evaluated, which gates blocked them, and what
    reason codes those gates fired.

    Schema of decision_traces.jsonl rows (per signal_logger.log_decision_trace):
        trade_date_ist:       "2024-10-07"
        snapshot_id:          "20241007_0915"
        final_outcome:        "blocked" | "hold" | "executed"
        primary_blocker_gate: "prefilter" | "stage1_threshold" | "stage2_direction" | ...
        flow_gates: [
            {gate_id, gate_group, status: "pass"|"blocked"|"hold", reason_code, ...},
            ...
        ]

    Returns:
        {
          "date": "2024-10-07",
          "mode": "replay",
          "total_traces": int,
          "outcomes": { "blocked": int, "hold": int, "executed": int },
          "primary_blocker_gates": [ {gate, count}, ... ],
          "blocking_reasons": [ {reason_code, count}, ... ],
          "narrative": "<one-sentence summary>",
        }
    """
    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"error": "date must be YYYY-MM-DD", "date": date}

    run_dir_path = Path(run_dir) if run_dir else _resolve_run_dir(mode)
    traces_path = run_dir_path / "decision_traces.jsonl"

    result = {
        "date": date,
        "mode": mode.strip().lower(),
        "traces_path": str(traces_path),
        "traces_path_exists": traces_path.exists(),
        "total_traces": 0,
        "outcomes": {},
        "primary_blocker_gates": [],
        "blocking_reasons": [],
        "narrative": "no decision_traces.jsonl on disk for this mode",
    }
    if not traces_path.exists():
        return result

    outcomes_c: Counter[str] = Counter()
    gates_c: Counter[str] = Counter()
    reasons_c: Counter[str] = Counter()
    matched = 0

    with traces_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            # Cheap pre-filter to avoid parsing JSON for non-matching dates.
            # Trade date appears verbatim in the line; if absent, skip.
            if date not in line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            td = str(t.get("trade_date_ist") or t.get("trade_date") or "")
            if td != date:
                continue
            matched += 1
            outcomes_c[str(t.get("final_outcome") or "?")] += 1
            gates_c[str(t.get("primary_blocker_gate") or "?")] += 1
            # First non-pass gate's reason — the actual proximate cause.
            for g in (t.get("flow_gates") or []):
                if g.get("status") != "pass":
                    rc = str(g.get("reason_code") or g.get("gate_id") or "?")
                    reasons_c[rc] += 1
                    break

    result["total_traces"] = matched
    result["outcomes"] = dict(outcomes_c)
    result["primary_blocker_gates"] = [
        {"gate": k, "count": v} for k, v in gates_c.most_common(top_n_gates)
    ]
    result["blocking_reasons"] = [
        {"reason_code": k, "count": v} for k, v in reasons_c.most_common(top_n_reasons)
    ]

    # Generate a short human narrative. Pipeline emits these outcome strings:
    #   entry_taken  → new position opened (the "trades" event from operator's POV)
    #   exit_taken   → position closed
    #   manage_only  → in position, just updating
    #   hold         → no entry but evaluated
    #   blocked      → gate rejected this snapshot
    # "executed" was retired before this code shipped; treat entry_taken as the trade-fired signal.
    entries = outcomes_c.get("entry_taken", 0) + outcomes_c.get("executed", 0)
    if matched == 0:
        result["narrative"] = (
            f"No decision traces for {date}. Either the strategy_app never processed "
            f"this date or decision_traces.jsonl was rotated."
        )
    elif entries > 0:
        top_reason = reasons_c.most_common(1)
        rc = top_reason[0][0] if top_reason else "?"
        rc_n = top_reason[0][1] if top_reason else 0
        result["narrative"] = (
            f"{matched} snapshots evaluated, {entries} produced trades (entry_taken). "
            f"Most-common reason code among non-pass gates: '{rc}' ({rc_n}). "
            f"Outcomes: {dict(outcomes_c)}."
        )
    else:
        top_gate = gates_c.most_common(1)
        top_reason = reasons_c.most_common(1)
        gate_str = f"{top_gate[0][0]} ({top_gate[0][1]} of {matched})" if top_gate else "?"
        reason_str = top_reason[0][0] if top_reason else "?"
        result["narrative"] = (
            f"{matched} snapshots evaluated, 0 produced trades. "
            f"Most-common blocking gate: {gate_str}. "
            f"Most-common reason code: '{reason_str}'. "
            f"This date is empty by model behavior — not a data gap."
        )

    return result


def _collapse_consecutive_identical(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive rows whose (outcome, blocker_gate, reason_code, entry_prob)
    are bit-identical. The dashboard caller uses this to expose the "Stage-1 is
    stuck at one probability for 17 minutes" signal as a single labelled row
    rather than 17 visually-identical rows the operator has to scroll past.

    The collapsed row keeps the FIRST row's metadata (snapshot_id, time, regime,
    full metrics block) and adds:
        time_end:     time of the last row in the run
        run_minutes:  count of merged rows (1 for un-collapsed rows)

    Two rows are considered identical if they share:
        outcome, blocker_gate, reason_code, AND the exact (bit-identical)
        entry_prob value from summary_metrics.

    Note we intentionally do NOT compare direction_up_prob or recipe_prob —
    those vary minute-to-minute even when Stage-1 is stuck, and we want the
    collapsing to highlight the Stage-1 freeze specifically.
    """
    if not rows:
        return rows
    out: list[dict[str, Any]] = []
    for r in rows:
        last = out[-1] if out else None
        if last is not None and (
            r.get("outcome") == last.get("outcome")
            and r.get("blocker_gate") == last.get("blocker_gate")
            and r.get("reason_code") == last.get("reason_code")
            and (r.get("metrics") or {}).get("entry_prob")
                == (last.get("metrics") or {}).get("entry_prob")
        ):
            last["time_end"] = r.get("time") or last.get("time_end")
            last["run_minutes"] = int(last.get("run_minutes") or 1) + 1
        else:
            nr = dict(r)
            nr["time_end"] = r.get("time")
            nr["run_minutes"] = 1
            out.append(nr)
    return out


def read_decision_timeline(
    mode: str = "replay",
    *,
    date: str,
    run_dir: Optional[Path] = None,
    limit: int = 500,
    offset: int = 0,
    outcome: Optional[str] = None,
    collapse: bool = False,
) -> dict[str, Any]:
    """Per-minute decision timeline for a date.

    Surfaces one row per snapshot — what was decided and why — so the
    operator can scroll a minute-by-minute view of "why no trade here?"
    instead of just aggregate counts.

    Each returned row is a slimmed-down decision_trace:
        {
          "time":        "09:15",          # IST HH:MM extracted from timestamp
          "snapshot_id": "20241007_0915",
          "outcome":     "blocked",        # final_outcome from the trace
          "blocker_gate":"prefilter",      # primary_blocker_gate (None if not blocked)
          "reason_code": "invalid_entry_phase",
          "message":     "invalid_entry_phase",
          "regime":      "SIDEWAYS",
          "metrics": {
            "entry_prob":     0.0,
            "recipe_prob":    0.0,
            "recipe_margin":  0.0,
            "direction_up_prob": 0.0,
          },
          "model_diagnostics": {
            "stage1": {"input_hash": "...", "non_null_count": 42, "output_prob": 0.56},
            "stage2": {"input_hash": "...", "non_null_count": 67, "output_prob": 0.52},
          },
        }

    Args:
        outcome: optional filter — only return rows whose final_outcome matches
                 (e.g. "blocked", "hold", "entry_taken"). None/empty = all.
        limit:   cap on returned rows after filtering. Hard cap 2000.
        offset:  rows to skip after filtering (for paging).
    """
    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"error": "date must be YYYY-MM-DD", "date": date}

    run_dir_path = Path(run_dir) if run_dir else _resolve_run_dir(mode)
    traces_path = run_dir_path / "decision_traces.jsonl"

    limit = max(0, min(int(limit), 2000))
    offset = max(0, int(offset))
    outcome_filter = (outcome or "").strip().lower() or None

    result: dict[str, Any] = {
        "date": date,
        "mode": mode.strip().lower(),
        "traces_path": str(traces_path),
        "traces_path_exists": traces_path.exists(),
        "total_for_date": 0,
        "matched_filter": 0,
        "returned": 0,
        "offset": offset,
        "limit": limit,
        "outcome_filter": outcome_filter,
        "decisions": [],
    }
    if not traces_path.exists():
        return result

    decisions: list[dict[str, Any]] = []
    total = 0
    matched = 0

    with traces_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            if date not in line:  # cheap pre-filter
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            td = str(t.get("trade_date_ist") or t.get("trade_date") or "")
            if td != date:
                continue
            total += 1
            oc = str(t.get("final_outcome") or "?")
            if outcome_filter and oc != outcome_filter:
                continue
            matched += 1
            if matched <= offset:
                continue
            if len(decisions) >= limit:
                continue  # keep counting total/matched for pagination metadata

            ts = str(t.get("timestamp") or "")
            # timestamp shape: "2024-10-07T09:15:00+05:30" — slice the HH:MM
            hhmm = ts[11:16] if len(ts) >= 16 else ""

            # First non-pass gate carries the proximate reason; fall back to summary.
            primary_gate = t.get("primary_blocker_gate")
            reason_code = ""
            message = ""
            for g in (t.get("flow_gates") or []):
                if g.get("status") != "pass":
                    reason_code = str(g.get("reason_code") or "")
                    message = str(g.get("message") or "")
                    break

            sm = t.get("summary_metrics") or {}
            rc = t.get("regime_context") or {}
            md = t.get("model_diagnostics") if isinstance(t.get("model_diagnostics"), dict) else {}
            decisions.append({
                "time": hhmm,
                "snapshot_id": t.get("snapshot_id"),
                "outcome": oc,
                "blocker_gate": primary_gate,
                "reason_code": reason_code,
                "message": message,
                "regime": rc.get("regime"),
                "metrics": {
                    "entry_prob": sm.get("entry_prob"),
                    "recipe_prob": sm.get("recipe_prob"),
                    "recipe_margin": sm.get("recipe_margin"),
                    "direction_up_prob": sm.get("direction_up_prob"),
                    "shadow_score": sm.get("shadow_score"),
                    "shadow_dir": sm.get("shadow_dir"),
                    "shadow_basis": sm.get("shadow_basis"),
                },
                "model_diagnostics": md,
            })

    if collapse:
        # Collapse AFTER paging — limit applies to raw rows, collapse just visually
        # compresses the slice the caller asked for. This keeps offsets stable.
        decisions = _collapse_consecutive_identical(decisions)

    result["total_for_date"] = total
    result["matched_filter"] = matched
    result["returned"] = len(decisions)
    result["collapsed"] = bool(collapse)
    result["decisions"] = decisions
    return result


def read_session_heatmap(
    mode: str = "replay",
    *,
    date: str,
    run_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Compact per-minute session view for the heatmap UI.

    Reads all entry-evaluation traces for a date and returns one compact row
    per traced minute — enough for the UI to paint a colored cell strip.

    Each row: {t, oc, gate, rc, sc, sd, sb, ep}
        t  — HH:MM (IST)
        oc — final_outcome
        gate — primary_blocker_gate
        rc — reason_code of first non-pass flow gate
        sc — shadow_score (float, + = CE, - = PE)
        sd — shadow_dir ("CE"|"PE")
        sb — shadow_basis string
        ep — entry_prob (float|null)
    """
    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"error": "date must be YYYY-MM-DD", "date": date, "rows": []}

    run_dir_path = Path(run_dir) if run_dir else _resolve_run_dir(mode)
    traces_path = run_dir_path / "decision_traces.jsonl"

    result: dict[str, Any] = {
        "date": date,
        "mode": mode.strip().lower(),
        "traces_path_exists": traces_path.exists(),
        "rows": [],
    }
    if not traces_path.exists():
        return result

    rows: list[dict[str, Any]] = []
    with traces_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            if date not in line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            td = str(t.get("trade_date_ist") or t.get("trade_date") or "")
            if td != date:
                continue
            # Skip position-management traces; only entry evaluations are useful for the heatmap.
            oc_val = str(t.get("final_outcome") or "")
            if oc_val in ("exit_taken", "manage_only"):
                continue
            ts = str(t.get("timestamp") or "")
            hhmm = ts[11:16] if len(ts) >= 16 else ""
            sm = t.get("summary_metrics") or {}
            primary_gate = t.get("primary_blocker_gate") or ""
            reason_code = ""
            for g in (t.get("flow_gates") or []):
                if g.get("status") != "pass":
                    reason_code = str(g.get("reason_code") or "")
                    break
            rows.append({
                "t": hhmm,
                "oc": str(t.get("final_outcome") or ""),
                "gate": primary_gate,
                "rc": reason_code,
                "sc": sm.get("shadow_score"),
                "sd": sm.get("shadow_dir"),
                "sb": sm.get("shadow_basis"),
                "ep": sm.get("entry_prob"),
            })

    result["rows"] = rows
    result["total"] = len(rows)
    return result


__all__ = ["read_strategy_current_state", "read_blocker_funnel", "read_decision_timeline", "read_session_heatmap"]
