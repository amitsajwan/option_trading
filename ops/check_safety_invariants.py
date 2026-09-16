#!/usr/bin/env python3
"""Safety invariant checker -- static, no live containers required.

Born 2026-09-16 out of a pattern found repeatedly during a disaster-recovery
and doc-cleanup audit: safety-critical defaults get set correctly for most
instruments, then silently missed for one when a new instrument is onboarded
or a pattern is copy-pasted. Concretely, this already happened twice on this
repo before this script existed:

  1. BankNifty/NIFTY sellers read *unnamespaced* EXECUTION_ADAPTER/
     SELLER_LIVE_ENABLED (falling through to .env.compose's global,
     live-by-default values) while FINNIFTY/SENSEX/MIDCPNIFTY were already
     safely namespaced with paper/0 defaults -- found 2026-09-15.
  2. FINNIFTY's buyer had no operator_halt entry in ensure_safe_defaults.sh
     at all -- found the same week, same audit.

Both were real-money-adjacent gaps that existed for a while before anyone
noticed, because nothing checked "do all N instruments follow the same
safe-default pattern" as a standing invariant. This script is that check.
It reads docker-compose*.yml and ensure_safe_defaults.sh directly -- no
running containers needed, safe to run in CI or before any deploy.

Exit code 0 = all invariants hold. Exit code 1 = at least one violation
(printed with enough detail to fix it).

Usage:
    python3 ops/check_safety_invariants.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Buyer service name pattern in docker-compose.gcp.yml. Variants that are
# NOT real-money-eligible live instances (replay/backtest/sim harnesses) are
# excluded -- they don't need an operator_halt file.
BUYER_SERVICE_RE = re.compile(r"^  (strategy_app(?:_[a-z]+)?):\s*$", re.MULTILINE)
BUYER_EXCLUDE_SUFFIXES = ("_historical", "_sim")

SELLER_SERVICE_RE = re.compile(r"^  (seller_app(?:_[a-z]+)?):\s*$", re.MULTILINE)

# A safety-critical env var line inside a service block, e.g.:
#   EXECUTION_ADAPTER: "${BANKNIFTY_EXECUTION_ADAPTER:-paper}"
#   SELLER_LIVE_ENABLED: "${NIFTY_SELLER_LIVE_ENABLED:-0}"
LIVE_VAR_LINE_RE = re.compile(
    r'^\s*(EXECUTION_ADAPTER|SELLER_LIVE_ENABLED):\s*"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}"',
    re.MULTILINE,
)

SAFE_DEFAULT = {"EXECUTION_ADAPTER": "paper", "SELLER_LIVE_ENABLED": "0"}


def _split_into_service_blocks(compose_text: str, service_re: re.Pattern) -> dict[str, str]:
    """Return {service_name: block_text} by slicing between top-level `  name:` headers."""
    matches = list(service_re.finditer(compose_text))
    blocks = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(compose_text)
        blocks[m.group(1)] = compose_text[start:end]
    return blocks


def check_seller_namespacing(violations: list[str]) -> None:
    path = REPO_ROOT / "docker-compose.seller.yml"
    if not path.exists():
        violations.append(f"MISSING FILE: {path} -- cannot check seller namespacing at all")
        return
    text = path.read_text(encoding="utf-8")
    blocks = _split_into_service_blocks(text, SELLER_SERVICE_RE)
    if not blocks:
        violations.append(f"{path}: found zero seller_app* service blocks -- regex may be stale")
        return

    for service, block in blocks.items():
        instrument_prefix = service.replace("seller_app", "").lstrip("_").upper() or None
        for var_match in LIVE_VAR_LINE_RE.finditer(block):
            var_name, ref_name, default = var_match.group(1), var_match.group(2), var_match.group(3)
            expected_prefix = f"{instrument_prefix}_" if instrument_prefix else ""
            if not ref_name.startswith(expected_prefix) or ref_name == var_name:
                violations.append(
                    f"{path} [{service}]: {var_name} reads unnamespaced/wrong var "
                    f"${{{ref_name}}} -- falls through to the GLOBAL .env.compose value "
                    f"instead of a {service}-specific one. This is exactly the "
                    f"2026-09-15 BankNifty/NIFTY seller bug."
                )
            if default != SAFE_DEFAULT[var_name]:
                violations.append(
                    f"{path} [{service}]: {var_name} default is "
                    f"{default!r}, expected {SAFE_DEFAULT[var_name]!r} (safe-by-default)."
                )


def check_buyer_halt_coverage(violations: list[str]) -> None:
    compose_path = REPO_ROOT / "docker-compose.gcp.yml"
    script_path = REPO_ROOT / "ops" / "gcp" / "ensure_safe_defaults.sh"
    if not compose_path.exists() or not script_path.exists():
        violations.append(
            f"MISSING FILE(S): need both {compose_path} and {script_path} to check halt coverage"
        )
        return

    compose_text = compose_path.read_text(encoding="utf-8")
    live_buyers = {
        m.group(1)
        for m in BUYER_SERVICE_RE.finditer(compose_text)
        if not m.group(1).endswith(BUYER_EXCLUDE_SUFFIXES)
    }

    script_text = script_path.read_text(encoding="utf-8")
    array_match = re.search(r"BUYER_SERVICES=\((.*?)\)", script_text, re.DOTALL)
    if not array_match:
        violations.append(f"{script_path}: could not find BUYER_SERVICES=(...) array -- script format changed?")
        return
    covered = set(array_match.group(1).split())

    missing = live_buyers - covered
    for svc in sorted(missing):
        violations.append(
            f"{script_path}: '{svc}' is a live buyer service in docker-compose.gcp.yml "
            f"but is MISSING from BUYER_SERVICES -- it will start with NO operator_halt "
            f"on a fresh deploy/restore. This is exactly the 2026-09-15 FINNIFTY gap."
        )

    stale = covered - live_buyers
    for svc in sorted(stale):
        violations.append(
            f"{script_path}: '{svc}' is in BUYER_SERVICES but no longer exists in "
            f"docker-compose.gcp.yml -- harmless but worth pruning (dead reference)."
        )


def main() -> int:
    violations: list[str] = []
    check_seller_namespacing(violations)
    check_buyer_halt_coverage(violations)

    if not violations:
        print("[check_safety_invariants] OK -- all seller namespacing and buyer halt-coverage invariants hold.")
        return 0

    print(f"[check_safety_invariants] {len(violations)} violation(s):\n")
    for v in violations:
        print(f"  - {v}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
