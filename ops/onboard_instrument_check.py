#!/usr/bin/env python3
"""Onboarding preflight checker — the pragmatic "one-click" for a new instrument.

Not compose generation (deliberately deferred — templating the compose of a
live-money system is a bigger, riskier change than this repo needs today).
This is validation: it prints exactly what's wired and what's still TODO for
one instrument, using the SAME check functions the CI lint runs
(ops/instrument_surfaces.py) — the checklist and the test can never disagree.

Usage:
    python ops/onboard_instrument_check.py --instrument FINNIFTY
    python ops/onboard_instrument_check.py --instrument MIDCPNIFTY   # not yet in the registry

Exit code: 0 if every surface is wired, 1 otherwise (CI/script-friendly).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from contracts_app.instruments import known_instruments  # noqa: E402
from ops.instrument_surfaces import surface_checks  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instrument", required=True, help="instrument name, e.g. FINNIFTY")
    args = ap.parse_args()
    inst = args.instrument.strip().upper()

    print(f"=== Onboarding preflight: {inst} ===\n")

    if inst not in known_instruments():
        print(f"NOT YET in the registry (known: {known_instruments()}).")
        print("Step 1: add an InstrumentSpec to contracts_app/instruments.py "
              "(index_security_id, lot_size, strike_step, expiry_cadence -- "
              "confirm all four against a LIVE Dhan scrip-master lookup, "
              "never guess).\n")
        print("Showing the checklist for every OTHER surface anyway, so you "
              "can prepare them in parallel:\n")

    checks = surface_checks(inst)
    n_ok = sum(1 for _, ok, _ in checks if ok)
    for surface, ok, detail in checks:
        mark = "PASS" if ok else "TODO"
        line = f"  [{mark}] {surface}"
        if not ok and detail:
            line += f"\n         -> {detail}"
        print(line)

    print(f"\n{n_ok}/{len(checks)} surfaces wired.")
    failed = [s for s, ok, _ in checks if not ok]
    if failed:
        print("\nNot ready. Fix the TODO items above, in the order listed "
              "(registry first, then WS/depth feeds, then compose, then "
              "deploy.sh/config-contract, then monitoring).")
        return 1
    print("\nAll known surfaces wired. Still required before real money:\n"
          "  - live Dhan sandbox order-path check (lot size, security-id resolution)\n"
          "  - a live paper-mode session (decision traces non-degenerate, "
          "probabilities match the offline distribution used for the entry threshold)\n"
          "  - ENTRY_ML_MIN_PROB computed empirically from the ACTUAL trained "
          "model's probability distribution -- never copy a sibling instrument's value\n"
          "See docs/ONBOARDING_INSTRUMENT.md for the full staged rollout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
