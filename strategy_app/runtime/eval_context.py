"""Per-tick evaluation context shared across strategies during one engine evaluate()."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from ..market.depth_context import DepthContext

_depth_ctx_var: ContextVar[Optional[DepthContext]] = ContextVar("strategy_eval_depth_ctx", default=None)


def set_depth_context(ctx: Optional[DepthContext]) -> None:
    _depth_ctx_var.set(ctx)


def get_depth_context() -> Optional[DepthContext]:
    return _depth_ctx_var.get()


def clear_depth_context() -> None:
    _depth_ctx_var.set(None)
