"""Feature-health report — "in seconds, what data is flowing and what is missing".

ONE place that defines every input group the strategy relies on, the snapshot
path it lives at, and whether the live snapshot actually populates it. Used by:

  - ``ops/gcp/verify_config.py`` (CLI: human-readable board)
  - the engine, which attaches the compact map to every decision trace under
    ``feature_health`` so a trace alone tells you whether a decision ran on full
    or degraded data.

Design rules:
  - Pure + null-safe. Never raises on a malformed snapshot (returns "missing").
  - Reads the SAME nested layout the live snapshot uses (verified against
    20260619_1529): futures_derived / chain_aggregates / atm_options / strikes /
    vix_context. If a producer renames a block this report flips to ✗ — that is
    the point (it would have caught the VIX-key + depth gaps).
  - A group can be REQUIRED (its absence degrades trading) or OPTIONAL (nice to
    have, e.g. depth before we start capturing it tomorrow).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def _get(d: Any, *keys: str) -> Any:
    """Walk nested dict keys, null-safe. Returns None on any miss."""
    cur = d
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def _present(v: Any) -> bool:
    """A value 'counts' as present if it is not None and not an empty container."""
    if v is None:
        return False
    if isinstance(v, (list, tuple, dict, str)) and len(v) == 0:
        return False
    return True


# Each probe: (label, required, getter(snapshot) -> value).
# `required=True` groups, if missing, mean the system is running degraded.
_PROBES: list[tuple[str, bool, Any]] = [
    ("futures_ohlcv",      True,  lambda s: _get(s, "futures_bar", "fut_close")),
    ("returns",            True,  lambda s: _get(s, "futures_derived", "fut_return_5m")),
    ("vwap",               True,  lambda s: _get(s, "futures_derived", "price_vs_vwap")),
    ("ema_stack",          True,  lambda s: _get(s, "futures_derived", "ema_order")),
    ("atr",                True,  lambda s: _get(s, "futures_derived", "atr_ratio")),
    ("compression_score",  True,  lambda s: _get(s, "futures_derived", "compression_score")),
    ("adx_14",             True,  lambda s: _get(s, "futures_derived", "adx_14")),
    ("vol_spike_ratio",    True,  lambda s: _get(s, "futures_derived", "vol_spike_ratio")),
    ("opening_range",      True,  lambda s: _get(s, "opening_range", "orh") or _get(s, "opening_range", "or_high")),
    ("pcr",                True,  lambda s: _get(s, "chain_aggregates", "pcr")),
    ("pcr_change_5m",      True,  lambda s: _get(s, "chain_aggregates", "pcr_change_5m")),
    ("max_pain",           True,  lambda s: _get(s, "chain_aggregates", "max_pain")),
    ("total_oi",           True,  lambda s: _get(s, "chain_aggregates", "total_ce_oi")),
    ("total_volume",       True,  lambda s: _get(s, "chain_aggregates", "total_ce_volume")),
    ("atm_premium",        True,  lambda s: _get(s, "atm_options", "atm_ce_close")),
    ("atm_oi",             True,  lambda s: _get(s, "atm_options", "atm_ce_oi")),
    ("atm_volume",         True,  lambda s: _get(s, "atm_options", "atm_ce_volume")),
    ("atm_iv",             True,  lambda s: _get(s, "atm_options", "atm_ce_iv")),
    ("strike_chain",       True,  lambda s: s.get("strikes") if isinstance(s, Mapping) else None),
    ("vix_current",        True,  lambda s: _get(s, "vix_context", "vix_current")),
    ("vix_intraday_chg",   True,  lambda s: _get(s, "vix_context", "vix_intraday_chg")),
    # OPTIONAL — order-book depth. Not captured until 2026-06-23; its absence must
    # NOT mark the system unhealthy, only flag the feature as unavailable.
    ("option_depth_bid",   False, lambda s: _depth_field(s, "ce_bid")),
    ("option_depth_ask",   False, lambda s: _depth_field(s, "ce_ask")),
]


def _depth_field(s: Any, field: str) -> Any:
    """Look for a bid/ask depth field on the ATM-ish strike row (if captured)."""
    strikes = s.get("strikes") if isinstance(s, Mapping) else None
    if not isinstance(strikes, list) or not strikes:
        return None
    row = strikes[len(strikes) // 2]  # middle ~ ATM
    if not isinstance(row, Mapping):
        return None
    return row.get(field)


def feature_health(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a structured availability report for one snapshot payload.

    Shape::

        {
          "snapshot_id": "...",
          "groups": {"pcr": {"present": True, "required": True}, ...},
          "required_present": 19, "required_total": 21,
          "optional_present": 0, "optional_total": 2,
          "missing_required": ["vix_intraday_chg"],   # the actionable list
          "degraded": True,                            # any required missing
        }
    """
    s = snapshot if isinstance(snapshot, Mapping) else {}
    groups: dict[str, dict[str, bool]] = {}
    missing_required: list[str] = []
    req_present = req_total = opt_present = opt_total = 0

    for label, required, getter in _PROBES:
        try:
            ok = _present(getter(s))
        except Exception:
            ok = False
        groups[label] = {"present": ok, "required": required}
        if required:
            req_total += 1
            if ok:
                req_present += 1
            else:
                missing_required.append(label)
        else:
            opt_total += 1
            if ok:
                opt_present += 1

    return {
        "snapshot_id": s.get("snapshot_id"),
        "groups": groups,
        "required_present": req_present,
        "required_total": req_total,
        "optional_present": opt_present,
        "optional_total": opt_total,
        "missing_required": missing_required,
        "degraded": bool(missing_required),
    }


def format_report(report: Mapping[str, Any]) -> str:
    """Human-readable board for the CLI verifier."""
    lines: list[str] = []
    rp, rt = report.get("required_present", 0), report.get("required_total", 0)
    op, ot = report.get("optional_present", 0), report.get("optional_total", 0)
    lines.append(f"  snapshot: {report.get('snapshot_id') or '<none>'}")
    lines.append(f"  required features: {rp}/{rt}    optional: {op}/{ot}")
    for label, info in (report.get("groups") or {}).items():
        mark = "✓" if info["present"] else ("✗ MISSING" if info["required"] else "– n/a")
        tag = "" if info["required"] else "  (optional)"
        lines.append(f"    {label:<20} {mark}{tag}")
    if report.get("missing_required"):
        lines.append(f"  ⚠ DEGRADED — missing required: {', '.join(report['missing_required'])}")
    else:
        lines.append("  ✓ all required features present")
    return "\n".join(lines)


__all__ = ["feature_health", "format_report"]
