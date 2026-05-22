"""Launch decoupled Stage-1 entry HPO (no direction/recipe training).

Manifest: configs/research/staged_dual_recipe.entry_s1_only_hpo_v1.json

VM:
    sudo bash /opt/option_trading/ops/gcp/run_entry_s1_only_hpo_vm.sh
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = (
    _REPO / "ml_pipeline_2" / "configs" / "research" / "staged_dual_recipe.entry_s1_only_hpo_v1.json"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(_DEFAULT_MANIFEST), help="Research manifest JSON path")
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
