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
    model = payload.get("model") or {}
    rollout = payload.get("rollout") or {}
    return {
        "engine": payload.get("engine"),
        "topic": payload.get("topic"),
        "strategy_profile_id": payload.get("strategy_profile_id"),
        "model_run_id": model.get("run_id"),
        "model_group": model.get("model_group"),
        "model_package_path": model.get("model_package_path"),
        "rollout_stage": rollout.get("stage"),
        "min_confidence": rollout.get("min_confidence"),
        "position_size_multiplier": rollout.get("position_size_multiplier"),
        "halt_consecutive_losses": rollout.get("halt_consecutive_losses"),
        "halt_daily_dd_pct": rollout.get("halt_daily_dd_pct"),
        "block_expiry": model.get("block_expiry"),
        "checked_at_ist": payload.get("checked_at_ist"),
    }


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


def read_decision_timeline(
    mode: str = "replay",
    *,
    date: str,
    run_dir: Optional[Path] = None,
    limit: int = 500,
    offset: int = 0,
    outcome: Optional[str] = None,
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
                },
                "model_diagnostics": md,
            })

    result["total_for_date"] = total
    result["matched_filter"] = matched
    result["returned"] = len(decisions)
    result["decisions"] = decisions
    return result


__all__ = ["read_strategy_current_state", "read_blocker_funnel", "read_decision_timeline"]
