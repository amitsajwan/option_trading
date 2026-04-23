"""Fast scenario runner for testing staged manifest variations without full training.

Use this to validate config changes, preview expected behavior, and iterate quickly
before launching expensive VM training jobs.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, Sequence

from ..contracts.manifests import load_and_resolve_manifest


DEFAULT_EXPIRY_BASE = Path(__file__).parent.parent.parent.parent / "configs" / "research" / "staged_dual_recipe.expiry_direction_v1.json"


def build_manifest(
    *,
    base_path: Path | None = None,
    overrides: Dict[str, Any] | None = None,
    run_name: str | None = None,
    bypass_stage2: bool | None = None,
    stage1_threshold_grid: Sequence[float] | None = None,
    stage3_threshold_grid: Sequence[float] | None = None,
    stage3_margin_grid: Sequence[float] | None = None,
    recipe_catalog_id: str | None = None,
    cost_per_trade: float | None = None,
) -> Dict[str, Any]:
    """Build a manifest by loading a base and applying overrides.

    This makes it trivial to test permutations without editing JSON files.
    """
    base = load_and_resolve_manifest(base_path or DEFAULT_EXPIRY_BASE, validate_paths=False)
    manifest: dict[str, Any] = json.loads(json.dumps(base, default=str))

    if overrides:
        _deep_update(manifest, overrides)

    if run_name is not None:
        manifest["outputs"]["run_name"] = str(run_name)

    if bypass_stage2 is not None:
        manifest["training"]["bypass_stage2"] = bool(bypass_stage2)

    if stage1_threshold_grid is not None:
        manifest["policy"]["stage1"]["threshold_grid"] = list(stage1_threshold_grid)

    if stage3_threshold_grid is not None:
        manifest["policy"]["stage3"]["threshold_grid"] = list(stage3_threshold_grid)

    if stage3_margin_grid is not None:
        manifest["policy"]["stage3"]["margin_grid"] = list(stage3_margin_grid)

    if recipe_catalog_id is not None:
        manifest["catalog"]["recipe_catalog_id"] = str(recipe_catalog_id)

    if cost_per_trade is not None:
        manifest["training"]["cost_per_trade"] = float(cost_per_trade)

    return manifest


def validate_manifest(manifest: Dict[str, Any], *, validate_paths: bool = False) -> Dict[str, Any]:
    """Validate a manifest dict and return the resolved version.

    Raises ManifestValidationError on invalid input.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(manifest, f, indent=2, default=str)
        f.flush()
        return load_and_resolve_manifest(Path(f.name), validate_paths=validate_paths)


def scenario_matrix(
    *,
    bypass_stage2_values: Sequence[bool] = (False, True),
    stage1_threshold_values: Sequence[Sequence[float]] = ((0.45, 0.5, 0.55, 0.6),),
    stage3_threshold_values: Sequence[Sequence[float]] = ((0.45, 0.5, 0.55, 0.6),),
    stage3_margin_values: Sequence[Sequence[float]] = ((0.02, 0.05, 0.1),),
) -> Sequence[Dict[str, Any]]:
    """Generate a cartesian product of manifest variations for batch testing."""
    manifests: list[dict[str, Any]] = []
    for bypass in bypass_stage2_values:
        for s1_thresh in stage1_threshold_values:
            for s3_thresh in stage3_threshold_values:
                for s3_margin in stage3_margin_values:
                    name_parts = [
                        "bypass" if bypass else "full",
                        f"s1t_{min(s1_thresh)}_{max(s1_thresh)}",
                        f"s3t_{min(s3_thresh)}_{max(s3_thresh)}",
                        f"s3m_{min(s3_margin)}_{max(s3_margin)}",
                    ]
                    run_name = "_".join(name_parts)
                    m = build_manifest(
                        run_name=run_name,
                        bypass_stage2=bypass,
                        stage1_threshold_grid=s1_thresh,
                        stage3_threshold_grid=s3_thresh,
                        stage3_margin_grid=s3_margin,
                    )
                    manifests.append(m)
    return manifests


def write_manifest(manifest: Dict[str, Any], path: Path) -> Path:
    """Write manifest to disk and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return path


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


__all__ = [
    "build_manifest",
    "validate_manifest",
    "scenario_matrix",
    "write_manifest",
]
