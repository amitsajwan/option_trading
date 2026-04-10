from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .staged.skew_diagnostic import (
    DEFAULT_SKEW_DIAGNOSTIC_FIXED_RECIPE_IDS,
    DEFAULT_SKEW_DIAGNOSTIC_TOP_FRACTIONS,
    run_stage12_skew_diagnostic,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose CE/PE directional skew in the Stage 1+2 pipeline. "
            "Reports breakdown at four levels: raw oracle, Stage1-positive, "
            "Stage1+2 actionable, and top-fraction slices. "
            "Identifies whether skew originates in market labels (Path A), "
            "model filtering (Path B), or shared ranking (Path C)."
        )
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Path to a completed staged research run directory",
    )
    parser.add_argument(
        "--top-fractions",
        nargs="+",
        type=float,
        default=list(DEFAULT_SKEW_DIAGNOSTIC_TOP_FRACTIONS),
        help="Top-trade fractions to evaluate, e.g. 0.5 0.33 0.25",
    )
    parser.add_argument(
        "--fixed-recipes",
        nargs="+",
        default=list(DEFAULT_SKEW_DIAGNOSTIC_FIXED_RECIPE_IDS),
        help="Fixed recipe ids to include in oracle universe, e.g. L3 L6",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="Output directory (default: <run-dir>/analysis/stage12_skew_diagnostic)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = run_stage12_skew_diagnostic(
        run_dir=Path(args.run_dir).resolve(),
        top_fractions=list(args.top_fractions),
        fixed_recipe_ids=list(args.fixed_recipes),
        output_root=(Path(args.output_root).resolve() if args.output_root else None),
    )
    print(json.dumps(payload.get("interpretation", {}), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
