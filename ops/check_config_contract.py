#!/usr/bin/env python3
"""Config contract checker — the wiring-gap detector.

Reads the LIVE environment of every strategy container (printenv = ground
truth, not what compose *should* have passed) and verifies it against
ops/config_contract_expected.json:

  - expect:                       key must equal this exact value
  - require_nonempty:             key must be present and non-empty
  - must_match_across_services:   value must be identical in every service
  - must_differ_across_services:  value must NOT be identical (catches a
                                  copy-paste service block reading the other
                                  instrument's model/topic)

Born 2026-07-07: EXIT_GIVEBACK_STOP_ENABLED was enabled in .env.compose but
the NIFTY compose service never passed the variable — a silent no-op this
check would have failed loudly.

Usage (on the runtime VM, host python3, stdlib only):
    sudo python3 ops/check_config_contract.py            # human report, exit 0/1
    sudo python3 ops/check_config_contract.py --quiet    # one line + exit code
"""
import json
import subprocess
import sys
from pathlib import Path

EXPECTED_PATH = Path(__file__).parent / "config_contract_expected.json"


def container_env(name: str) -> dict:
    out = subprocess.run(
        ["docker", "exec", name, "printenv"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"docker exec {name} printenv failed: {out.stderr.strip()[:200]}")
    env = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def main() -> int:
    quiet = "--quiet" in sys.argv
    contract = json.loads(EXPECTED_PATH.read_text())
    services = contract.get("services", {})

    envs: dict[str, dict] = {}
    failures: list[str] = []

    for cname in services:
        try:
            envs[cname] = container_env(cname)
        except Exception as exc:
            failures.append(f"[{cname}] UNREACHABLE: {exc}")

    for cname, spec in services.items():
        env = envs.get(cname)
        if env is None:
            continue
        for key, want in (spec.get("expect") or {}).items():
            got = env.get(key)
            if got is None:
                failures.append(f"[{cname}] {key} MISSING (expected '{want}')")
            elif got != str(want):
                failures.append(f"[{cname}] {key}='{got}' (expected '{want}')")
        for key in spec.get("require_nonempty") or []:
            if not (env.get(key) or "").strip():
                failures.append(f"[{cname}] {key} missing or empty (required non-empty)")

    live = {c: e for c, e in envs.items() if e is not None}
    if len(live) >= 2:
        names = sorted(live)
        for key in contract.get("must_match_across_services") or []:
            vals = {c: live[c].get(key) for c in names}
            if len(set(vals.values())) > 1:
                detail = ", ".join(f"{c.split('-')[-2]}='{v}'" for c, v in vals.items())
                failures.append(f"[cross] {key} differs across services: {detail}")
        for key in contract.get("must_differ_across_services") or []:
            vals = [live[c].get(key) for c in names]
            if len(set(vals)) == 1:
                failures.append(
                    f"[cross] {key} is IDENTICAL across services ('{vals[0]}') — "
                    "one service is likely reading the other instrument's config"
                )

    if failures:
        print(f"CONFIG CONTRACT: FAIL ({len(failures)} violation(s))")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    n_keys = sum(len(s.get("expect", {})) + len(s.get("require_nonempty", [])) for s in services.values())
    print(f"CONFIG CONTRACT: PASS ({len(live)}/{len(services)} services, {n_keys} keys checked)")
    if not quiet:
        for cname in sorted(live):
            spec = services[cname]
            for key in spec.get("expect") or {}:
                print(f"  ✓ [{cname}] {key}={live[cname].get(key)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
