"""Instrument-hardcoding lint — the "never onboard by hand again" guardrail.

Born 2026-08-04 after FINNIFTY's onboarding: ~25 code sites hardcoded
instrument names/lists outside contracts_app.instruments, several of which
were live-money bugs (NIFTY-weekly expiry baked into FINNIFTY's backfilled
DTE; FINNIFTY_LOT_SIZE set in compose but read by nothing; zero
feature-health/liveness/contract coverage for the new instrument; dashboard
routes 400-rejecting it). Each check below turns one of those failure
classes into a CI failure.

All checks derive from contracts_app.known_instruments() — the day
instrument #4 is added to the registry, every check automatically demands
its full surface set.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from contracts_app.instruments import FNO_SEGMENT_WS_CODE, INSTRUMENTS, known_instruments, get_instrument
from ops.instrument_surfaces import (
    AST_ALLOWLIST,
    EXCHANGE_SEGMENT_ALLOWLIST,
    REPO,
    ast_scan_repo,
    exchange_segment_scan_repo,
    surface_problems,
)


class TestNoNewHardcodedInstrumentLogic(unittest.TestCase):
    def test_ast_scan_finds_no_registry_bypassing_literals(self) -> None:
        violations = ast_scan_repo()
        self.assertEqual(
            violations, [],
            "\n\nRegistry-bypassing instrument hardcoding found. Derive from "
            "contracts_app.get_instrument()/known_instruments() (or, for a "
            "deliberate alias table / one-shot study script, add the file to "
            "ops/instrument_surfaces.AST_ALLOWLIST with a reason):\n  "
            + "\n  ".join(violations),
        )

    def test_allowlist_entries_still_exist(self) -> None:
        # A stale allowlist silently widens the exemption surface.
        stale = [rel for rel in AST_ALLOWLIST if not (REPO / rel).exists()]
        self.assertEqual(stale, [], f"AST_ALLOWLIST paths no longer exist: {stale}")


class TestExchangeSegmentIsRegistryDerived(unittest.TestCase):
    """Born 2026-08-07 onboarding SENSEX (BSE) -- the first non-NSE
    instrument. InstrumentSpec.fno_segment existed the whole time but was
    dead in the live path (order placement, historical/quote fetch, WS
    subscription all hardcoded "NSE_FNO"). Closes that gap category for
    whatever exchange instrument #5 lands on."""

    def test_exchange_segment_is_registry_derived(self) -> None:
        violations = exchange_segment_scan_repo()
        self.assertEqual(
            violations, [],
            "\n\nBare exchange-segment string literal found outside the registry. "
            "Derive from contracts_app.get_instrument(name).fno_segment instead "
            "(or add a deliberate exemption to "
            "ops/instrument_surfaces.EXCHANGE_SEGMENT_ALLOWLIST with a reason):\n  "
            + "\n  ".join(violations),
        )

    def test_exchange_segment_allowlist_entries_still_exist(self) -> None:
        stale = [rel for rel in EXCHANGE_SEGMENT_ALLOWLIST if not (REPO / rel).exists()]
        self.assertEqual(stale, [], f"EXCHANGE_SEGMENT_ALLOWLIST paths no longer exist: {stale}")

    def test_ws_segment_codes_cover_every_registered_fno_segment(self) -> None:
        # If instrument #5 lands on a third exchange without a numeric
        # ws-feed code registered, it must fail CI here -- not silently
        # inherit NSE's code (2) via dhan_ws_feed.py's fno_segment_ws_code().
        missing = sorted(
            {get_instrument(name).fno_segment for name in known_instruments()}
            - set(FNO_SEGMENT_WS_CODE)
        )
        self.assertEqual(
            missing, [],
            f"fno_segment(s) {missing} have no numeric WS code in "
            "contracts_app.instruments.FNO_SEGMENT_WS_CODE",
        )


class TestEveryInstrumentHasItsFullSurface(unittest.TestCase):
    def test_all_registry_instruments_fully_wired(self) -> None:
        problems: list[str] = []
        for inst in known_instruments():
            problems.extend(surface_problems(inst))
        self.assertEqual(
            problems, [],
            "\n\nInstrument surfaces missing (a registry instrument is not "
            "fully wired -- this is exactly how FINNIFTY shipped with zero "
            "monitoring coverage):\n  " + "\n  ".join(problems),
        )


class TestRegistryPipelineParityBidirectional(unittest.TestCase):
    def test_every_registry_instrument_in_pipeline(self) -> None:
        # The existing test_registry_parity_with_pipeline iterates the
        # PIPELINE dict -- a registry-only instrument passed silently.
        # This direction closes that gap.
        try:
            import importlib
            mod = importlib.import_module("ml_pipeline_2.scripts.dhan_data_pipeline")
        except Exception:
            self.skipTest("ml_pipeline_2 not importable in this env")
        for name in known_instruments():
            self.assertIn(
                name, mod.INSTRUMENTS,
                f"{name} is in the contracts registry but missing from "
                "ml_pipeline_2/scripts/dhan_data_pipeline.py INSTRUMENTS",
            )

    def test_registry_specs_are_complete(self) -> None:
        for name, spec in INSTRUMENTS.items():
            self.assertTrue(spec.index_security_id.strip(), f"{name}: empty index_security_id")
            self.assertGreater(spec.lot_size, 0, f"{name}: lot_size must be > 0")
            self.assertGreater(spec.strike_step, 0, f"{name}: strike_step must be > 0")
            self.assertIn(spec.expiry_cadence, ("weekly", "monthly"),
                          f"{name}: unknown expiry_cadence {spec.expiry_cadence!r}")


if __name__ == "__main__":
    unittest.main()
