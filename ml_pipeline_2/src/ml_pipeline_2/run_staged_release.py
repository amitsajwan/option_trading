from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .staged.publish import release_staged_run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the staged dual-recipe release flow: train/reuse, assess, publish, and optional GCS sync.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", help="Path to a staged manifest JSON to run end-to-end")
    source.add_argument("--run-dir", help="Existing completed staged run directory to assess/release")
    parser.add_argument("--run-output-root", help="Optional explicit run directory when using --config")
    parser.add_argument(
        "--run-reuse-mode",
        choices=["fail_if_exists", "resume", "restart"],
        default="fail_if_exists",
        help="How to treat an explicit run-output-root when using --config",
    )
    parser.add_argument("--model-group", required=True, help="Published model group, e.g. banknifty_futures/h15_tp_auto")
    parser.add_argument("--profile-id", required=True, help="Published runtime profile id")
    parser.add_argument("--model-bucket-url", help="Optional GCS destination root, e.g. gs://my-bucket/published_models")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = release_staged_run(
        config=(Path(args.config).resolve() if args.config else None),
        run_dir=(Path(args.run_dir).resolve() if args.run_dir else None),
        run_output_root=(Path(args.run_output_root).resolve() if args.run_output_root else None),
        run_reuse_mode=str(args.run_reuse_mode),
        model_group=args.model_group,
        profile_id=args.profile_id,
        model_bucket_url=(str(args.model_bucket_url).strip() if args.model_bucket_url else None),
    )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
