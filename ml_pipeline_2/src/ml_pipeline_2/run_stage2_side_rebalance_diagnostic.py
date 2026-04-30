from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .staged.stage2_side_rebalance import run_stage2_side_rebalance_diagnostic


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a Stage 2 side-rebalance diagnostic. Sweeps asymmetric CE/PE "
            "thresholds around the current policy, measures CE/PE capture versus "
            "the Stage1-positive oracle baseline, and scores fixed L3/L6 economics."
        )
    )
    parser.add_argument("--run-dir", required=True, help="Path to a completed staged research run directory")
    parser.add_argument("--trade-threshold-grid", nargs="+", type=float, default=None, help="Optional Stage 2 trade-threshold grid")
    parser.add_argument("--ce-threshold-grid", nargs="+", type=float, default=None, help="Optional CE threshold grid")
    parser.add_argument("--pe-threshold-grid", nargs="+", type=float, default=None, help="Optional PE threshold grid")
    parser.add_argument("--min-edge-grid", nargs="+", type=float, default=None, help="Optional Stage 2 min-edge grid")
    parser.add_argument("--fixed-recipes", nargs="+", default=["L3", "L6"], help="Fixed recipe ids to score, e.g. L3 L6")
    parser.add_argument("--output-root", default=None, help="Output directory (default: <run-dir>/analysis/stage2_side_rebalance_diagnostic)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = run_stage2_side_rebalance_diagnostic(
        run_dir=Path(args.run_dir).resolve(),
        fixed_recipe_ids=list(args.fixed_recipes),
        trade_threshold_grid=args.trade_threshold_grid,
        ce_threshold_grid=args.ce_threshold_grid,
        pe_threshold_grid=args.pe_threshold_grid,
        min_edge_grid=args.min_edge_grid,
        output_root=(Path(args.output_root).resolve() if args.output_root else None),
    )
    print(json.dumps(payload.get("winners", {}), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
