from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .staged.stage2_feature_signal import run_stage2_feature_signal_diagnostic


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Stage 2 feature-signal gate. Reuses the staged oracle/window "
            "builders, measures CE vs PE feature separation on validation and holdout, "
            "and emits a binary YES/NO memo for Story 2."
        )
    )
    parser.add_argument("--run-dir", required=True, help="Path to a completed staged research run directory")
    parser.add_argument("--fixed-recipes", nargs="+", default=["L3", "L6"], help="Fixed recipe ids used for oracle construction")
    parser.add_argument("--output-root", default=None, help="Output directory (default: <run-dir>/analysis/stage2_feature_signal_diagnostic)")
    parser.add_argument("--min-effect-size", type=float, default=0.10, help="Minimum absolute Cohen's d for a stable feature")
    parser.add_argument("--max-p-value", type=float, default=0.05, help="Maximum Mann-Whitney p-value for a stable feature")
    parser.add_argument("--min-stable-features", type=int, default=3, help="Minimum number of stable features required for a YES verdict")
    parser.add_argument("--stage1-positive-only", action="store_true", help="Restrict the analysis set to Stage 1 positive oracle rows only")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = run_stage2_feature_signal_diagnostic(
        run_dir=Path(args.run_dir).resolve(),
        fixed_recipe_ids=list(args.fixed_recipes),
        output_root=(Path(args.output_root).resolve() if args.output_root else None),
        min_effect_size=float(args.min_effect_size),
        max_p_value=float(args.max_p_value),
        min_stable_features=int(args.min_stable_features),
        stage1_positive_only=bool(args.stage1_positive_only),
    )
    print(
        json.dumps(
            {
                "source_run_id": payload.get("source_run_id"),
                "analysis_scope": payload.get("analysis_scope"),
                "stable_feature_count": payload.get("stable_feature_count"),
                "signal_exists": payload.get("signal_exists"),
                "verdict": payload.get("verdict"),
                "paths": payload.get("paths"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
