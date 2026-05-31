"""Shared helpers for all stage consumers."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

SENTINEL_TYPE = "sentinel"


def parse_payload_from_fields(fields: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Decode the JSON 'payload' field written by RedisEventBus.publish()."""
    raw = fields.get("payload")
    if not raw:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def is_sentinel(fields: Mapping[str, Any]) -> bool:
    return str(fields.get("type") or "").lower() == SENTINEL_TYPE


def now_iso() -> str:
    from contracts_app import isoformat_ist
    return isoformat_ist()


def safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def snapshot_trade_date(snapshot: dict[str, Any]) -> Optional[date]:
    sc = snapshot.get("session_context") if isinstance(snapshot.get("session_context"), dict) else {}
    raw = str(sc.get("date") or snapshot.get("trade_date") or "").strip()
    if len(raw) >= 10:
        try:
            parts = raw[:10].split("-")
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            pass
    return None


def atm_premium_for_direction(snapshot: dict[str, Any], direction: str) -> Optional[float]:
    """Return the ATM option LTP for the given direction from the snapshot."""
    atm = snapshot.get("atm_options") if isinstance(snapshot.get("atm_options"), dict) else {}
    key = "atm_ce_ltp" if direction == "CE" else "atm_pe_ltp"
    val = atm.get(key)
    if val is None:
        # try strikes list
        strikes = snapshot.get("strikes") if isinstance(snapshot.get("strikes"), list) else []
        for row in strikes:
            if not isinstance(row, dict):
                continue
            if row.get("is_atm") and row.get("option_type", "").upper() == direction:
                val = row.get("ltp")
                break
    return safe_float(val)
