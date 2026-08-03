#!/usr/bin/env python3
"""Direction-resolver liveness report — the "which inputs actually fire" check
that the 2026-07-07 config contract (value-correctness) and the 21 feature-
health probes (snapshot-field NaN rates) both miss: a WEIGHTED or conditional
input can be individually well-configured and still contribute to zero live
decisions, silently, forever (2026-07-19: NIFTY's composite depth signal,
weight 1.1 -- the single heaviest input -- sat dead behind
DEPTH_FEED_ENABLED=0 for weeks; nothing logged the skip).

Two independent checks, both cheap:
  1. STATIC — for each strategy container, which ML_ENTRY_DIRECTION_MODE is
     actually running, and (if composite) whether its weighted signals' own
     runtime dependencies are enabled. Mirrors config_contract's
     weight_requires rules but as a standalone, readable report.
  2. OBSERVED — the last N real POSITION_OPEN events' direction_mode /
     direction_sources fields (persisted since 2026-07-19), so the static
     config claim is cross-checked against what actually happened.

Usage (on the runtime VM, host python3 + pymongo):
    sudo python3 ops/verify_direction_liveness.py
"""
import json
import subprocess
import sys
from collections import Counter

try:
    from pymongo import MongoClient
except ImportError:
    MongoClient = None

def _registry_services() -> dict:
    """Registry-derived container -> instrument map (2026-08-04: the old
    hardcoded 2-entry dict skipped FINNIFTY entirely)."""
    try:
        import sys
        from pathlib import Path
        repo = Path(__file__).resolve().parents[1]
        if str(repo) not in sys.path:
            sys.path.insert(0, str(repo))
        from contracts_app.instruments import known_instruments
        from contracts_app.sim_namespace import PRIMARY_INSTRUMENT
        out = {}
        for inst in known_instruments():
            suffix = "" if inst == PRIMARY_INSTRUMENT else f"_{inst.lower()}"
            out[f"option_trading-strategy_app{suffix}-1"] = inst
        return out
    except Exception:
        return {
            "option_trading-strategy_app-1": "BANKNIFTY",
            "option_trading-strategy_app_nifty-1": "NIFTY",
        }


SERVICES = _registry_services()

# Mirrors the weight_requires rules in config_contract_expected.json — kept
# inline here so this script has no dependency on that file's schema.
COMPOSITE_WEIGHTS = [
    ("ENTRY_DIR_W_DEPTH", 1.1, "DEPTH_FEED_ENABLED", "1", None),
    ("ENTRY_DIR_W_ML", 0.20, None, None, "DIRECTION_ML_MODEL_PATH"),
    ("ENTRY_DIR_W_MOMENTUM_5M", 1.0, None, None, None),
    ("ENTRY_DIR_W_MOMENTUM_15M", 0.55, None, None, None),
    ("ENTRY_DIR_W_VWAP", 0.65, None, None, None),
    ("ENTRY_DIR_W_VIX", 0.75, None, None, None),
    ("ENTRY_DIR_W_IV_SKEW", 0.5, None, None, None),
    ("ENTRY_DIR_W_OR_TRAP", 0.9, None, None, None),
    ("ENTRY_DIR_W_PCR", 0.4, None, None, None),
]


def container_env(name: str) -> dict:
    out = subprocess.run(["docker", "exec", name, "printenv"],
                          capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def static_check() -> list[str]:
    lines = []
    for cname, instrument in SERVICES.items():
        try:
            env = container_env(cname)
        except Exception as exc:
            lines.append(f"{instrument}: UNREACHABLE ({exc})")
            continue
        mode = env.get("ML_ENTRY_DIRECTION_MODE", "composite")
        lines.append(f"{instrument} ({cname}): mode={mode}")
        if mode == "direction_ml":
            path = env.get("DIRECTION_ML_MODEL_PATH", "")
            lines.append(f"  model_path={'SET: ' + path if path else 'EMPTY -- direction_ml mode with no model = every bar abstains'}")
        elif mode == "composite":
            for wkey, wdefault, gkey, gval, rnonempty in COMPOSITE_WEIGHTS:
                try:
                    w = float(env.get(wkey, wdefault))
                except (TypeError, ValueError):
                    w = wdefault
                if w <= 0:
                    lines.append(f"  {wkey}=0 (disabled)")
                    continue
                dead = False
                if gkey and env.get(gkey) != gval:
                    dead = True
                    reason = f"{gkey}={env.get(gkey)!r} (needs {gval!r})"
                elif rnonempty and not (env.get(rnonempty) or "").strip():
                    dead = True
                    reason = f"{rnonempty} is empty"
                else:
                    reason = ""
                tag = "DEAD" if dead else "live"
                lines.append(f"  {wkey}={w:g} [{tag}]" + (f" -- {reason}" if dead else ""))
    return lines


def observed_check(n: int = 200) -> list[str]:
    if MongoClient is None:
        return ["(pymongo not available, skipping observed check)"]
    lines = []
    db = MongoClient("mongodb://localhost:27017")["trading_ai"]
    pairs = []
    for cname, inst in SERVICES.items():
        suffix = "" if inst == "BANKNIFTY" else f"_{inst.lower()}"
        pairs.append((f"strategy_positions{suffix}", inst))
    for coll, instrument in pairs:
        try:
            rows = list(db[coll].find(
                {"event": "POSITION_OPEN"},
                {"decision_metrics": 1},
            ).sort("$natural", -1).limit(n))
        except Exception as exc:
            lines.append(f"{instrument}: query failed ({exc})")
            continue
        modes = Counter()
        sources = Counter()
        n_no_sources = 0
        for r in rows:
            dm = r.get("decision_metrics") or {}
            modes[dm.get("direction_mode", "(not recorded)")] += 1
            raw = dm.get("direction_sources")
            if raw:
                for tag in raw.split(","):
                    fam = tag.split(":")[0]
                    sources[fam] += 1
            elif dm.get("direction_mode") == "composite":
                n_no_sources += 1
        lines.append(f"{instrument}: last {len(rows)} real entries, mode breakdown: {dict(modes)}")
        if sources:
            lines.append(f"  observed composite component fires: {dict(sources.most_common())}")
        if n_no_sources:
            lines.append(f"  {n_no_sources} composite entries have NO recorded sources (pre-2026-07-19 trades, or the field didn't persist -- investigate)")
    return lines


def main() -> int:
    print("=== STATIC: configured direction mode + weighted-input liveness ===")
    for line in static_check():
        print(line)
    print("\n=== OBSERVED: what the last real trades actually recorded ===")
    for line in observed_check():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
