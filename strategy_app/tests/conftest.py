"""pytest configuration for strategy_app tests.

EXIT_POLICY_STACK_ENABLED defaults ON in tracker.py (2026-06-24 change).
Pre-stack tests that construct PositionTracker and assert specific bar counts
or no-exit behaviour would break because ThesisFail fires at bar 3 with zero MFE.

This autouse fixture pins the env to 0 so legacy tests are unaffected.
Tests that explicitly need the stack enabled should set EXIT_POLICY_STACK_ENABLED=1
via monkeypatch or patch.dict *inside* the test body — that overrides the setdefault.
"""
import os
import pytest


@pytest.fixture(autouse=True)
def _exit_stack_isolation():
    prev = os.environ.get("EXIT_POLICY_STACK_ENABLED")
    os.environ.setdefault("EXIT_POLICY_STACK_ENABLED", "0")
    yield
    if prev is None:
        os.environ.pop("EXIT_POLICY_STACK_ENABLED", None)
    else:
        os.environ["EXIT_POLICY_STACK_ENABLED"] = prev


@pytest.fixture(autouse=True)
def _clear_instrument_env(monkeypatch):
    """Reset instrument-scoped env vars so tests don't leak STRATEGY_INSTRUMENT
    or STRATEGY_LOT_SIZE into one another (e.g. a NIFTY test leaving lot_size=75
    and breaking a later BankNifty test)."""
    monkeypatch.delenv("STRATEGY_INSTRUMENT", raising=False)
    monkeypatch.delenv("STRATEGY_LOT_SIZE", raising=False)
