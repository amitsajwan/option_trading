"""Launch direction-only staged research with Optuna HPO.

Uses the full ml_pipeline_2 research stack (walk-forward CV, model catalog,
feature-set search, threshold policy grid) — not the quick single-XGB script.

Manifest: configs/research/staged_dual_recipe.direction_only_hpo_v1.json
  - Label: direction_market_up_all_v1 (CE vs PE, no entry_label gate)
  - Stage2: Optuna 40 trials/model, 5 feature sets, 11 model families
  - Stage1: reused from prior deep_hpo_c1 run (configurable in manifest)

Run on ML VM (hours; use nohup):
    cd /opt/option_trading
    export PYTHONPATH=/opt/option_trading
    nohup .venv/bin/python -u -m ml_pipeline_2.scripts.run_direction_only_hpo \
        > /tmp/direction_only_hpo.log 2>&1 &
    tail -f /tmp/direction_only_hpo.log

Or validate manifest only:
    .venv/bin/python -m ml_pipeline_2.scripts.run_direction_only_hpo --validate-only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = (
    _REPO / "ml_pipeline_2" / "configs" / "research" / "staged_dual_recipe.direction_only_hpo_v1.json"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--config",
        default=str(_DEFAULT_MANIFEST),
        help="Research manifest JSON path",
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--print-resolved-config", action="store_true")
    parser.add_argument("--run-output-root", help="Optional explicit run directory")
    parser.add_argument(
        "--run-reuse-mode",
        choices=["fail_if_exists", "resume", "restart"],
        default="fail_if_exists",
    )
    args = parser.parse_args(argv)

    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))

    from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
    from ml_pipeline_2.experiment_control.runner import run_research, validate_runtime_environment

    resolved = load_and_resolve_manifest(Path(args.config), validate_paths=True)
    if args.print_resolved_config:
        print(json.dumps(resolved, indent=2, default=str))
    if args.validate_only:
        validate_runtime_environment(resolved)
        print("Manifest OK:", args.config)
        return 0

    summary = run_research(
        resolved,
        run_output_root=(Path(args.run_output_root).resolve() if args.run_output_root else None),
        run_reuse_mode=str(args.run_reuse_mode),
    )
    print(json.dumps(summary, indent=2, default=str))
    status = str(summary.get("status") or "").lower()
    return 0 if status in {"completed", "success", "ok"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
