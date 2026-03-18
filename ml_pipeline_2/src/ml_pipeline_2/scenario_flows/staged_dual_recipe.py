from __future__ import annotations

from ..experiment_control.state import RunContext
from ..staged.pipeline import run_staged_research


def run_staged_dual_recipe(ctx: RunContext):
    return run_staged_research(ctx)
