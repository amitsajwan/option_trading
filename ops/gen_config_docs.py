"""Generate the config reference table from the registry — ops/gen_config_docs.py

Writes docs/strategy_platform/CONFIG_REGISTRY_TABLE.md from
strategy_app/config/registry.py, so the config docs can never go stale: the
registry is the single source, this is its rendering.

Usage:
    python ops/gen_config_docs.py            # regenerate the table
    python ops/gen_config_docs.py --check    # exit 1 if out of date (CI guard)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_app.config.registry import REGISTRY

OUT = Path(__file__).resolve().parents[1] / "docs" / "strategy_platform" / "CONFIG_REGISTRY_TABLE.md"

_GROUP_ORDER = ["execution", "profile", "entry", "direction", "regime", "exit", "strike", "risk"]


def render() -> str:
    lines = [
        "# Config Registry Table (generated)",
        "",
        "> **Do not edit by hand.** Generated from `strategy_app/config/registry.py`",
        "> by `ops/gen_config_docs.py`. Values live in `ops/strategy_config.yml`;",
        "> both live and sim read them via `strategy_app/config/loader.py`.",
        "> See `CONFIG_CONSOLIDATION_PLAN.md`.",
        "",
        f"Total keys: **{len(REGISTRY)}** "
        f"({sum(1 for k in REGISTRY if k.sim_overridable)} sim-overridable).",
        "",
    ]
    groups = sorted({k.group for k in REGISTRY}, key=lambda g: (_GROUP_ORDER.index(g) if g in _GROUP_ORDER else 99, g))
    for group in groups:
        keys = [k for k in REGISTRY if k.group == group]
        lines.append(f"## {group}")
        lines.append("")
        lines.append("| YAML path | Env var | Type | Default | Sim? | Meaning |")
        lines.append("|---|---|---|---|:--:|---|")
        for k in keys:
            default = k.format(k.default)
            if default == "":
                default = "*(empty)*"
            sim = "✓" if k.sim_overridable else ""
            desc = k.description.replace("|", "\\|")
            lines.append(
                f"| `{k.yaml_path}` | `{k.env_var}` | {k.type} | `{default}` | {sim} | {desc} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if file is out of date")
    args = ap.parse_args()

    content = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current.strip() != content.strip():
            print("CONFIG_REGISTRY_TABLE.md is OUT OF DATE — run: python ops/gen_config_docs.py")
            return 1
        print("CONFIG_REGISTRY_TABLE.md is up to date.")
        return 0

    OUT.write_text(content + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(REGISTRY)} keys)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
