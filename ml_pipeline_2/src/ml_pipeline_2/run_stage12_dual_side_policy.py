from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .staged.dual_side_policy import (
    DEFAULT_DUAL_SIDE_POLICY_CE_FRACTIONS,
    DEFAULT_DUAL_SIDE_POLICY_FIXED_RECIPE_IDS,
    DEFAULT_DUAL_SIDE_POLICY_PE_FRACTIONS,
    run_stage12_dual_side_policy,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Select CE and PE books independently from the Stage1+Stage2 actionable set, "
            "then evaluate fixed L3/L6 execution on validation and holdout."
        )
    )
    parser.add_argument("--run-dir", required=True, help="Path to a completed staged research run directory")
    parser.add_argument("--ce-fraction-grid", nargs="+", type=float, default=list(DEFAULT_DUAL_SIDE_POLICY_CE_FRACTIONS))
    parser.add_argument("--pe-fraction-grid", nargs="+", type=float, default=list(DEFAULT_DUAL_SIDE_POLICY_PE_FRACTIONS))
    parser.add_argument("--fixed-recipes", nargs="+", default=list(DEFAULT_DUAL_SIDE_POLICY_FIXED_RECIPE_IDS))
    parser.add_argument("--validation-min-trades-soft", type=int, default=None)
    parser.add_argument("--side-share-min", type=float, default=None)
    parser.add_argument("--side-share-max", type=float, default=None)
    parser.add_argument("--prefer-profit-factor-min", type=float, default=None)
    parser.add_argument("--output-root", default=None, help="Output directory (default: <run-dir>/analysis/stage12_dual_side_policy)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    validation_policy = {
        key: value
        for key, value in {
            "validation_min_trades_soft": args.validation_min_trades_soft,
            "side_share_min": args.side_share_min,
            "side_share_max": args.side_share_max,
            "prefer_profit_factor_min": args.prefer_profit_factor_min,
        }.items()
        if value is not None
    }
    payload = run_stage12_dual_side_policy(
        run_dir=Path(args.run_dir).resolve(),
        ce_fraction_grid=list(args.ce_fraction_grid),
        pe_fraction_grid=list(args.pe_fraction_grid),
        fixed_recipe_ids=list(args.fixed_recipes),
        validation_policy=validation_policy,
        output_root=(Path(args.output_root).resolve() if args.output_root else None),
    )
    print(
        json.dumps(
            {
                "analysis_kind": payload["analysis_kind"],
                "winner": payload["winner"],
                "paths": payload["paths"],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
