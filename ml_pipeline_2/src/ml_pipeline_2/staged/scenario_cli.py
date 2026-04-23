"""CLI for quickly generating, diffing, and launching scenario variations.

Examples:
    # Generate a bypass_stage2 scenario and show diff from baseline
    python -m ml_pipeline_2.staged.scenario_cli --bypass-stage2 --diff

    # Generate a scenario with narrower grids
    python -m ml_pipeline_2.staged.scenario_cli --bypass-stage2 --stage1-thresholds 0.5 0.55 --stage3-margins 0.02 0.05 --write-config /tmp/my_scenario.json

    # Launch on VM
    python -m ml_pipeline_2.staged.scenario_cli --bypass-stage2 --launch-vm
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Sequence

from .scenario_runner import build_manifest, scenario_matrix, validate_manifest, write_manifest
from .config_diff import diff_manifests, print_diff


def _parse_float_list(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scenario generation and diff tool for staged manifests.")
    parser.add_argument("--bypass-stage2", action="store_true", help="Set bypass_stage2=True")
    parser.add_argument("--stage1-thresholds", type=_parse_float_list, help="Comma-separated stage1 threshold grid")
    parser.add_argument("--stage3-thresholds", type=_parse_float_list, help="Comma-separated stage3 threshold grid")
    parser.add_argument("--stage3-margins", type=_parse_float_list, help="Comma-separated stage3 margin grid")
    parser.add_argument("--cost-per-trade", type=float, help="Override cost_per_trade")
    parser.add_argument("--run-name", type=str, help="Override run_name")
    parser.add_argument("--recipe-catalog", type=str, help="Override recipe catalog id")
    parser.add_argument("--diff", action="store_true", help="Show diff from baseline (bypass_stage2=False)")
    parser.add_argument("--validate", action="store_true", help="Validate the generated manifest")
    parser.add_argument("--write-config", type=str, help="Write generated manifest to file")
    parser.add_argument("--launch-vm", action="store_true", help="Launch on VM via BatchLauncher (requires tmux)")
    parser.add_argument("--batch", action="store_true", help="Generate a full scenario matrix and show count")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.batch:
        manifests = scenario_matrix(
            bypass_stage2_values=(False, True) if not args.bypass_stage2 else (True,),
            stage1_threshold_values=(tuple(args.stage1_thresholds),) if args.stage1_thresholds else ((0.45, 0.5, 0.55, 0.6),),
            stage3_threshold_values=(tuple(args.stage3_thresholds),) if args.stage3_thresholds else ((0.45, 0.5, 0.55, 0.6),),
            stage3_margin_values=(tuple(args.stage3_margins),) if args.stage3_margins else ((0.02, 0.05, 0.1),),
        )
        print(f"Generated {len(manifests)} scenario variations.")
        for i, m in enumerate(manifests):
            print(f"  {i+1}. {m['outputs']['run_name']}")
        return 0

    kwargs: dict[str, Any] = {"bypass_stage2": args.bypass_stage2}
    if args.stage1_thresholds:
        kwargs["stage1_threshold_grid"] = args.stage1_thresholds
    if args.stage3_thresholds:
        kwargs["stage3_threshold_grid"] = args.stage3_thresholds
    if args.stage3_margins:
        kwargs["stage3_margin_grid"] = args.stage3_margins
    if args.cost_per_trade is not None:
        kwargs["cost_per_trade"] = args.cost_per_trade
    if args.run_name:
        kwargs["run_name"] = args.run_name
    if args.recipe_catalog:
        kwargs["recipe_catalog_id"] = args.recipe_catalog

    manifest = build_manifest(**kwargs)

    if args.diff:
        baseline = build_manifest(bypass_stage2=False)
        print("Diff from baseline:")
        print_diff(diff_manifests(baseline, manifest))
        print()

    if args.validate:
        try:
            validate_manifest(manifest, validate_paths=False)
            print("Validation: PASSED")
        except Exception as e:
            print(f"Validation: FAILED - {type(e).__name__}: {e}")
            return 1

    if args.write_config:
        path = Path(args.write_config)
        write_manifest(manifest, path)
        print(f"Wrote manifest to: {path}")

    if args.launch_vm:
        try:
            from .batch_launcher import BatchLauncher
        except ImportError as e:
            print(f"Cannot launch on VM: {e}")
            return 1
        launcher = BatchLauncher()
        launcher.queue(manifest, run_name=manifest["outputs"]["run_name"])
        results = launcher.launch_all()
        for r in results:
            print(f"  {r['run_name']}: {r['status']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
