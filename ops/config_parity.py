"""Config parity check — ops/config_parity.py

Compares the resolved ops/strategy_config.yml (the new source of truth) against a
.env.compose file, for every registry key. Run this BEFORE flipping live to
yaml_wins so you know exactly what (if anything) would change.

Usage:
    python ops/config_parity.py                         # vs ./.env.compose
    python ops/config_parity.py /opt/option_trading/.env.compose
    python ops/config_parity.py --strict                # exit 1 if any real diff

A "real diff" ignores numeric-equal repr differences (0.10 vs 0.1).
Exit 0 = parity (safe to flip). Exit 1 = differences found (review first).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_app.config.loader import resolve
from strategy_app.config.registry import BY_ENV


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def same(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("env_file", nargs="?", default=".env.compose")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any real diff")
    args = ap.parse_args()

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"ERROR: {env_path} not found")
        return 2

    yaml_cfg = resolve()
    env_cfg = parse_env_file(env_path)

    diffs, only_env, missing = [], [], []
    for env_var in sorted(BY_ENV):
        y = yaml_cfg.get(env_var, "")
        e = env_cfg.get(env_var)
        if e is None:
            missing.append(env_var)            # in registry, not in .env.compose
        elif not same(e, y):
            diffs.append((env_var, e, y))

    print(f"=== Config parity: {env_path} vs ops/strategy_config.yml ===\n")
    if diffs:
        print(f"REAL DIFFERENCES ({len(diffs)}) -- YAML would CHANGE these on cutover:")
        w = max(len(k) for k, _, _ in diffs)
        for k, e, y in diffs:
            print(f"  {k.ljust(w)}  env={e!r:>12}  ->  yaml={y!r}")
        print()
    else:
        print("No real differences — YAML matches .env.compose. SAFE to flip.\n")

    if missing:
        print(f"In registry but absent from .env.compose ({len(missing)}) -- "
              f"YAML default will apply: {', '.join(missing)}\n")

    return 1 if (args.strict and diffs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
