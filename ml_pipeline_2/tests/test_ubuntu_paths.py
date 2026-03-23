from __future__ import annotations

import json
import shutil
from pathlib import Path

from ml_pipeline_2.contracts.manifests import load_and_resolve_manifest
from ml_pipeline_2.tests.helpers import build_staged_parquet_root


def _copy_default_json(source: Path, dest: Path) -> Path:
    payload = json.loads(source.read_text(encoding="utf-8"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return dest


def _seed_external_parquet_cache(workspace: Path) -> Path:
    parquet_src = build_staged_parquet_root(workspace / "source_data")
    parquet_dest = workspace / ".data" / "ml_pipeline" / "parquet_data"
    shutil.copytree(parquet_src, parquet_dest, dirs_exist_ok=True)
    return parquet_dest


def test_staged_manifest_resolves_relative_paths_from_config_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    parquet_root = _seed_external_parquet_cache(workspace)
    config_path = _copy_default_json(
        Path("ml_pipeline_2/configs/research/staged_dual_recipe.default.json"),
        workspace / "ml_pipeline_2" / "configs" / "research" / "staged_dual_recipe.default.json",
    )
    resolved = load_and_resolve_manifest(config_path, validate_paths=True)

    assert Path(resolved["inputs"]["parquet_root"]) == parquet_root
    assert resolved["inputs"]["support_dataset"] == "snapshots_ml_flat"
    assert Path(resolved["outputs"]["artifacts_root"]) == workspace / "ml_pipeline_2" / "artifacts" / "research"


def test_active_ubuntu_docs_and_configs_do_not_contain_windows_absolute_paths() -> None:
    targets = [
        Path("ml_pipeline_2/README.md"),
        Path("ml_pipeline_2/docs/ubuntu_gcp_runbook.md"),
        Path("ml_pipeline_2/pyproject.toml"),
        *sorted(Path("ml_pipeline_2/configs/research").glob("*.json")),
    ]
    offenders = []
    for path in targets:
        text = path.read_text(encoding="utf-8")
        if "C:\\" in text or "C:/" in text:
            offenders.append(str(path))
    assert offenders == []
