from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .staged.counterfactual import (
    DEFAULT_FIXED_RECIPE_IDS,
    DEFAULT_TOP_FRACTIONS,
    analyze_stage12_counterfactual,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate top-confidence Stage1+Stage2 holdout trades from a completed staged run against fixed recipes and an oracle upper bound."
    )
    parser.add_argument("--run-dir", required=True, help="Path to a completed staged research run directory")
    parser.add_argument(
        "--top-fractions",
        nargs="+",
        type=float,
        default=list(DEFAULT_TOP_FRACTIONS),
        help="Top-trade fractions to evaluate, e.g. 1.0 0.5 0.25 0.1",
    )
    parser.add_argument(
        "--fixed-recipes",
        nargs="+",
        default=list(DEFAULT_FIXED_RECIPE_IDS),
        help="Fixed recipe ids to compare, e.g. L3 L6",
    )
    parser.add_argument(
        "--output-root",
        help="Optional explicit analysis output directory; defaults to <run-dir>/analysis/stage12_counterfactual",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = analyze_stage12_counterfactual(
        run_dir=Path(args.run_dir).resolve(),
        top_fractions=list(args.top_fractions),
        fixed_recipe_ids=list(args.fixed_recipes),
        output_root=(Path(args.output_root).resolve() if args.output_root else None),
    )
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
