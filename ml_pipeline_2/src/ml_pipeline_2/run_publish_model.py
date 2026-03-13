from __future__ import annotations

import argparse
import json
import sys

from .publishing import publish_recovery_run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish a completed ml_pipeline_2 recovery run for runtime consumption.")
    parser.add_argument("--run-dir", required=True, help="Completed fo_expiry_aware_recovery run directory")
    parser.add_argument("--model-group", required=True, help="Published model group, e.g. banknifty_futures/h15_tp_auto")
    parser.add_argument("--profile-id", required=True, help="Published runtime profile id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = publish_recovery_run(
        run_dir=args.run_dir,
        model_group=args.model_group,
        profile_id=args.profile_id,
    )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
