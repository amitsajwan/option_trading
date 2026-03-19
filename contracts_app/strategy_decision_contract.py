from __future__ import annotations

import math
import re
from typing import Any, Optional

ENGINE_MODES = frozenset({"deterministic", "ml_pure"})
DECISION_MODES = frozenset({"rule_vote", "ml_staged"})
ALIAS_REASON_CODES = {
    "entry_warmup": "entry_warmup_block",
    "warmup_blocked": "entry_warmup_block",
    "policy_allow": "policy_allowed",
    "times_top": "time_stop",
}
_REASON_PREFIX = re.compile(r"\breason=([a-zA-Z0-9_:-]+)")
_REASON_PROBES = (
    "low_edge_conflict",
    "below_threshold",
    "ce_above_threshold",
    "pe_above_threshold",
    "dual_pass_ce_higher",
    "dual_pass_pe_higher",
    "feature_stale",
    "feature_incomplete",
    "sideways_block",
    "avoid_regime",
    "regime_low_confidence",
    "below_min_confidence",
    "direction_conflict",
    "entry_warmup_block",
)
_FLOAT_TOKEN_TEMPLATE = r"\b{token}=([-+]?[0-9]*\.?[0-9]+)"


def _safe_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return float(out)


def normalize_engine_mode(value: Any) -> Optional[str]:
    token = str(value or "").strip().lower()
    if token in ENGINE_MODES:
        return token
    return None


def normalize_decision_mode(value: Any) -> Optional[str]:
    token = str(value or "").strip().lower()
    if token in DECISION_MODES:
        return token
    return None


def normalize_reason_code(value: Any) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    token = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_")
    if not token:
        return None
    return ALIAS_REASON_CODES.get(token, token)


def extract_reason_code_from_text(text: Any) -> Optional[str]:
    value = str(text or "").strip()
    if not value:
        return None
    prefixed = _REASON_PREFIX.search(value)
    if prefixed:
        return normalize_reason_code(prefixed.group(1))
    if value.startswith("ml_pure_hold:"):
        return normalize_reason_code(value.split(":", 1)[1])
    lowered = value.lower()
    for item in _REASON_PROBES:
        if item in lowered:
            return item
    return None


def parse_metric_token(raw: Any, metric_key: str) -> Optional[float]:
    text = str(raw or "").strip()
    key = str(metric_key or "").strip()
    if not text or not key:
        return None
    token = f"{key}="
    idx = text.find(token)
    if idx >= 0:
        tail = text[idx + len(token) :].strip()
        if tail:
            end = len(tail)
            for separator in (",", " ", "|"):
                pos = tail.find(separator)
                if pos >= 0:
                    end = min(end, pos)
            parsed = _safe_float(tail[:end].strip())
            if parsed is not None:
                return parsed
    pattern = _FLOAT_TOKEN_TEMPLATE.format(token=re.escape(key))
    match = re.search(pattern, text)
    if not match:
        return None
    return _safe_float(match.group(1))


def merge_decision_metrics(*sources: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key, raw in source.items():
            value = _safe_float(raw)
            if value is None:
                continue
            out[str(key)] = float(value)
    return out


__all__ = [
    "ENGINE_MODES",
    "DECISION_MODES",
    "ALIAS_REASON_CODES",
    "normalize_engine_mode",
    "normalize_decision_mode",
    "normalize_reason_code",
    "extract_reason_code_from_text",
    "parse_metric_token",
    "merge_decision_metrics",
]
