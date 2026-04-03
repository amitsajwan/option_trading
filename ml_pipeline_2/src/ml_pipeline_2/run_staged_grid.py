from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .contracts.manifests import STAGED_GRID_KIND, load_and_resolve_manifest
from .staged.grid import run_staged_grid


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the staged training grid and rank one winner across research lanes.")
    parser.add_argument("--config", required=True, help="Path to a staged grid manifest JSON")
    parser.add_argument("--run-output-root", help="Optional explicit grid output directory")
    parser.add_argument(
        "--run-reuse-mode",
        choices=["fail_if_exists", "resume", "restart"],
        default="fail_if_exists",
        help="How to treat an explicit run-output-root when it already has artifacts",
    )
    parser.add_argument("--model-group", required=True, help="Base model group prefix, e.g. banknifty_futures/h15_tp_auto")
    parser.add_argument("--profile-id", required=True, help="Published runtime profile id")
    parser.add_argument("--model-bucket-url", help="Optional GCS destination root for winner publish, e.g. gs://my-bucket/published_models")
    parser.add_argument("--publish-winner", action="store_true", help="Publish the selected winner after ranking if it is publishable")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    resolved = load_and_resolve_manifest(Path(args.config), validate_paths=True)
    if str(resolved.get("experiment_kind") or "") != STAGED_GRID_KIND:
        raise ValueError(f"--config must resolve to experiment_kind={STAGED_GRID_KIND}")
    payload = run_staged_grid(
        resolved,
        model_group=args.model_group,
        profile_id=args.profile_id,
        run_output_root=(Path(args.run_output_root).resolve() if args.run_output_root else None),
        run_reuse_mode=str(args.run_reuse_mode),
        publish_winner=bool(args.publish_winner),
        model_bucket_url=(str(args.model_bucket_url).strip() if args.model_bucket_url else None),
    )
    print(json.dumps(payload, indent=2, default=str))
    return 1 if str(payload.get("status") or "").strip().lower() == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
