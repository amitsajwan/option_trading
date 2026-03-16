from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .publishing import release_recovery_run


def _parse_threshold_grid(values: Optional[Sequence[str]]) -> Optional[list[float]]:
    if not values:
        return None
    out: list[float] = []
    for value in values:
        for token in str(value).split(","):
            token = str(token).strip()
            if not token:
                continue
            threshold = float(token)
            if threshold < 0.0 or threshold > 1.0:
                raise ValueError(f"threshold out of range [0,1]: {threshold}")
            out.append(float(threshold))
    deduped = sorted({round(float(value), 10) for value in out})
    if not deduped:
        return None
    return [float(x) for x in deduped]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the supported ml_pipeline_2 recovery release flow: train/reuse, sweep, publish, and optional GCS sync."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", help="Path to a recovery manifest JSON to run end-to-end")
    source.add_argument("--run-dir", help="Existing completed recovery run directory to assess/release")
    parser.add_argument("--run-output-root", help="Optional explicit run directory when using --config")
    parser.add_argument("--model-group", required=True, help="Published model group, e.g. banknifty_futures/h15_tp_auto")
    parser.add_argument("--profile-id", required=True, help="Published runtime profile id")
    parser.add_argument(
        "--threshold-source",
        choices=("training", "threshold_sweep_recommended"),
        default="threshold_sweep_recommended",
        help="Threshold source used for release assessment and publishing",
    )
    parser.add_argument(
        "--threshold-grid",
        nargs="*",
        help="Optional threshold grid values for the sweep. Supports repeated values or comma-separated tokens.",
    )
    parser.add_argument("--skip-threshold-sweep", action="store_true", help="Skip threshold sweep generation")
    parser.add_argument("--model-bucket-url", help="Optional GCS destination root, e.g. gs://my-bucket/published_models")
    parser.add_argument(
        "--allow-unsafe-publish",
        action="store_true",
        help="Allow publishing even when the release assessment marks the run non-publishable",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = release_recovery_run(
        config=(Path(args.config).resolve() if args.config else None),
        run_dir=(Path(args.run_dir).resolve() if args.run_dir else None),
        run_output_root=(Path(args.run_output_root).resolve() if args.run_output_root else None),
        model_group=args.model_group,
        profile_id=args.profile_id,
        threshold_source=str(args.threshold_source),
        threshold_grid=_parse_threshold_grid(args.threshold_grid),
        model_bucket_url=(str(args.model_bucket_url).strip() if args.model_bucket_url else None),
        allow_unsafe_publish=bool(args.allow_unsafe_publish),
        skip_threshold_sweep=bool(args.skip_threshold_sweep),
    )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
