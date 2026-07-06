"""Per-tick evaluation context shared across strategies during one engine evaluate()."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from ..market.depth_context import DepthContext

_depth_ctx_var: ContextVar[Optional[DepthContext]] = ContextVar("strategy_eval_depth_ctx", default=None)


def set_depth_context(ctx: Optional[DepthContext]) -> None:
    _depth_ctx_var.set(ctx)


def get_depth_context() -> Optional[DepthContext]:
    return _depth_ctx_var.get()


def clear_depth_context() -> None:
    _depth_ctx_var.set(None)


# ── Entry-model diagnostics ────────────────────────────────────────────────────
# The entry model records its computed probability here EVERY bar — including bars
# where it declined (prob < threshold) and therefore emitted no vote. The engine
# reads it when building the decision trace so the FULL prob distribution (fired +
# declined) is captured, enabling true separation analysis (S7).
_entry_diag_var: ContextVar[Optional[dict[str, Any]]] = ContextVar("strategy_eval_entry_diag", default=None)


def set_entry_diag(diag: Optional[dict[str, Any]]) -> None:
    _entry_diag_var.set(diag)


def get_entry_diag() -> Optional[dict[str, Any]]:
    return _entry_diag_var.get()


def clear_entry_diag() -> None:
    _entry_diag_var.set(None)


def no_votes_blocker_reason() -> str:
    """Return a specific blocker name when _collect_votes returned [].

    Reads the entry-model diag recorded this bar (set_entry_diag) and converts
    the raw ML outcome into a trace-level gate name that's immediately readable:

      ml_entry_nan_warmup       — NaN count exceeded max (data not ready)
      ml_entry_below_threshold  — prob computed but < threshold
      ml_entry_cost_gate        — prob fired, cost-ratio gate blocked
      ml_entry_direction_none   — prob fired, cost passed, direction resolved None
      no_strategy_votes         — fallback (diag absent or unrecognised state)

    The goal: trace primary_blocker_gate is self-explanatory without code diving.
    """
    diag = _entry_diag_var.get()
    if not isinstance(diag, dict):
        return "no_strategy_votes"
    if diag.get("error") == "prediction_failed":
        return "ml_entry_nan_warmup"
    fired = diag.get("fired")
    if fired is False:
        return "ml_entry_below_threshold"
    if fired is True:
        cg = diag.get("cost_gate")
        if isinstance(cg, dict) and not cg.get("ok"):
            return "ml_entry_cost_gate"
        if diag.get("direction_none"):
            return "ml_entry_direction_none"
    return "no_strategy_votes"
