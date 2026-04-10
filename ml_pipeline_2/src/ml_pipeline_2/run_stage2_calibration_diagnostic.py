from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .staged.stage2_calibration import (
    DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS,
    run_stage2_calibration_diagnostic,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep Stage 2 thresholds on an existing staged run and report "
            "side alignment plus fixed-recipe economics for validation and holdout."
        )
    )
    parser.add_argument("--run-dir", required=True, help="Path to a completed staged research run directory")
    parser.add_argument(
        "--fixed-recipes",
        nargs="+",
        default=list(DEFAULT_STAGE2_CALIBRATION_FIXED_RECIPE_IDS),
        help="Fixed recipe ids to evaluate on selected trades, e.g. L3 L6",
    )
    parser.add_argument("--trade-threshold-grid", nargs="+", type=float, default=None)
    parser.add_argument("--ce-threshold-grid", nargs="+", type=float, default=None)
    parser.add_argument("--pe-threshold-grid", nargs="+", type=float, default=None)
    parser.add_argument("--min-edge-grid", nargs="+", type=float, default=None)
    parser.add_argument("--validation-min-trades-soft", type=int, default=None)
    parser.add_argument("--side-share-min", type=float, default=None)
    parser.add_argument("--side-share-max", type=float, default=None)
    parser.add_argument("--prefer-profit-factor-min", type=float, default=None)
    parser.add_argument("--output-root", default=None, help="Output directory (default: <run-dir>/analysis/stage2_calibration_diagnostic)")
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
    payload = run_stage2_calibration_diagnostic(
        run_dir=Path(args.run_dir).resolve(),
        fixed_recipe_ids=list(args.fixed_recipes),
        trade_threshold_grid=None if args.trade_threshold_grid is None else list(args.trade_threshold_grid),
        ce_threshold_grid=None if args.ce_threshold_grid is None else list(args.ce_threshold_grid),
        pe_threshold_grid=None if args.pe_threshold_grid is None else list(args.pe_threshold_grid),
        min_edge_grid=None if args.min_edge_grid is None else list(args.min_edge_grid),
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
