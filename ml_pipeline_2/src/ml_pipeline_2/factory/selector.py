from __future__ import annotations

from typing import Any, Dict, Optional, Sequence


def _metric_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def select_winner(candidates: Sequence[Dict[str, Any]], *, strategy: str) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    strategy_name = str(strategy).strip().lower()
    if strategy_name != "publishable_economics_v1":
        raise ValueError(f"unsupported ranking strategy: {strategy}")
    ranked = sorted(
        [dict(item) for item in candidates],
        key=lambda item: (
            _metric_float(item.get("profit_factor"), default=float("-inf")),
            _metric_float(item.get("net_return_sum"), default=float("-inf")),
            _metric_float(item.get("stage2_roc_auc"), default=float("-inf")),
            str(item.get("lane_id") or ""),
        ),
        reverse=True,
    )
    return ranked[0]


__all__ = ["select_winner"]
