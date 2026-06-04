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
