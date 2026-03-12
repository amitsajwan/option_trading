from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts.manifests import load_and_resolve_manifest
from .experiment_control.runner import run_research


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ml_pipeline_2 research manifests.")
    parser.add_argument("--config", required=True, help="Path to manifest JSON")
    parser.add_argument("--validate-only", action="store_true", help="Validate the manifest and exit")
    parser.add_argument("--print-resolved-config", action="store_true", help="Print the resolved config JSON and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    resolved = load_and_resolve_manifest(Path(args.config), validate_paths=True)
    if args.print_resolved_config:
        print(json.dumps(resolved, indent=2, default=str))
        if args.validate_only:
            return 0
    if args.validate_only:
        return 0
    summary = run_research(resolved)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
