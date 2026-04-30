from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .factory.runner import WorkflowRunner, resolve_workflow_root
from .factory.spec import load_workflow_spec


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the snapshot training factory workflow.")
    parser.add_argument("--spec", required=True, help="Path to a factory workflow JSON spec")
    parser.add_argument("--output-root", help="Optional parent directory for the workflow run root")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    spec = load_workflow_spec(Path(args.spec))
    workflow_root = resolve_workflow_root(spec, (Path(args.output_root).resolve() if args.output_root else None))
    payload = WorkflowRunner(spec, workflow_root).run()
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload.get("status") == "publishable_found" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
