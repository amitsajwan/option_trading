from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts.manifests import STAGED_KIND
from .publishing import publish_recovery_run, publish_staged_run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish a completed ml_pipeline_2 runtime run for runtime consumption.")
    parser.add_argument("--run-dir", required=True, help="Completed staged or fo_expiry_aware_recovery run directory")
    parser.add_argument("--model-group", required=True, help="Published model group, e.g. banknifty_futures/h15_tp_auto")
    parser.add_argument("--profile-id", required=True, help="Published runtime profile id")
    parser.add_argument(
        "--threshold-source",
        choices=("training", "threshold_sweep_recommended"),
        default="training",
        help="Threshold source for the published runtime profile",
    )
    parser.add_argument(
        "--allow-unsafe-publish",
        action="store_true",
        help="Allow publishing a completed run even when release assessment marks it non-publishable",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    summary_path = Path(args.run_dir).resolve() / "summary.json"
    payload_dict = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    if str(payload_dict.get("experiment_kind") or "").strip() == STAGED_KIND:
        if bool(args.allow_unsafe_publish):
            raise ValueError("staged publish does not support --allow-unsafe-publish")
        payload = publish_staged_run(
            run_dir=args.run_dir,
            model_group=args.model_group,
            profile_id=args.profile_id,
        )
    else:
        payload = publish_recovery_run(
            run_dir=args.run_dir,
            model_group=args.model_group,
            profile_id=args.profile_id,
            threshold_source=args.threshold_source,
            allow_unsafe_publish=bool(args.allow_unsafe_publish),
        )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
