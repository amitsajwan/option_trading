from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .staged.confidence_execution_policy import (
    DEFAULT_CONFIDENCE_EXECUTION_POLICY_FIXED_RECIPE_IDS,
    DEFAULT_CONFIDENCE_EXECUTION_POLICY_SIDE_CAPS,
    DEFAULT_CONFIDENCE_EXECUTION_POLICY_TOP_FRACTIONS,
    run_stage12_confidence_execution_policy,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search fixed-recipe confidence execution policies with optional side caps from a completed Stage1+Stage2 staged run."
    )
    parser.add_argument("--run-dir", required=True, help="Path to a completed staged research run directory")
    parser.add_argument(
        "--top-fractions",
        nargs="+",
        type=float,
        default=list(DEFAULT_CONFIDENCE_EXECUTION_POLICY_TOP_FRACTIONS),
        help="Validation/holdout top-trade fractions to test, e.g. 0.5 0.33 0.25",
    )
    parser.add_argument(
        "--fixed-recipes",
        nargs="+",
        default=list(DEFAULT_CONFIDENCE_EXECUTION_POLICY_FIXED_RECIPE_IDS),
        help="Fixed recipe ids to compare, e.g. L3 L6",
    )
    parser.add_argument(
        "--side-cap-grid",
        nargs="+",
        type=float,
        default=list(DEFAULT_CONFIDENCE_EXECUTION_POLICY_SIDE_CAPS),
        help="Maximum share allowed for either side after trimming, e.g. 1.0 0.85 0.75 0.70",
    )
    parser.add_argument("--validation-min-trades-soft", type=int, default=50, help="Soft minimum validation trade count for ranking")
    parser.add_argument("--side-share-min", type=float, default=0.30, help="Soft minimum long-share for ranking")
    parser.add_argument("--side-share-max", type=float, default=0.70, help="Soft maximum long-share for ranking")
    parser.add_argument("--prefer-profit-factor-min", type=float, default=1.0, help="Soft profit-factor target for ranking")
    parser.add_argument(
        "--output-root",
        help="Optional explicit analysis output directory; defaults to <run-dir>/analysis/stage12_confidence_execution_policy",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = run_stage12_confidence_execution_policy(
        run_dir=Path(args.run_dir).resolve(),
        top_fractions=list(args.top_fractions),
        fixed_recipe_ids=list(args.fixed_recipes),
        side_cap_grid=list(args.side_cap_grid),
        validation_policy={
            "validation_min_trades_soft": int(args.validation_min_trades_soft),
            "side_share_min": float(args.side_share_min),
            "side_share_max": float(args.side_share_max),
            "prefer_profit_factor_min": float(args.prefer_profit_factor_min),
            "prefer_non_negative_returns": True,
        },
        output_root=(Path(args.output_root).resolve() if args.output_root else None),
    )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
