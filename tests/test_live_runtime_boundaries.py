from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_supported_live_runtime_python_modules_do_not_import_ml_pipeline() -> None:
    paths = [
        "contracts_app/logging_utils.py",
        "contracts_app/strategy_decision_contract.py",
        "contracts_app/time_utils.py",
        "ingestion_app/api_service.py",
        "ingestion_app/runtime.py",
        "persistence_app/main_snapshot_consumer.py",
        "persistence_app/main_strategy_consumer.py",
        "persistence_app/strategy_health.py",
        "snapshot_app/live_ml_flat.py",
        "snapshot_app/live_validate.py",
        "strategy_app/engines/pure_ml_engine.py",
        "strategy_app/main.py",
        "market_data_dashboard/live_strategy_monitor_service.py",
        "market_data_dashboard/app.py",
    ]
    for rel_path in paths:
        text = _read(rel_path)
        assert "from ml_pipeline import" not in text
        assert "from ml_pipeline." not in text
        assert "import ml_pipeline" not in text
        assert "ml_pipeline/src" not in text


def test_supported_live_runtime_imports_resolve() -> None:
    modules = [
        "contracts_app.logging_utils",
        "contracts_app.strategy_decision_contract",
        "contracts_app.time_utils",
        "persistence_app.main_strategy_consumer",
        "persistence_app.strategy_health",
        "snapshot_app.live_ml_flat",
        "snapshot_app.live_validate",
        "strategy_app.engines.pure_ml_engine",
        "strategy_app.main",
        "market_data_dashboard.live_strategy_monitor_service",
        "market_data_dashboard.app",
    ]
    for module_name in modules:
        __import__(module_name)


def test_supported_live_runtime_dockerfiles_do_not_copy_ml_pipeline_or_set_it_on_pythonpath() -> None:
    dockerfiles = [
        "ingestion_app/Dockerfile",
        "snapshot_app/Dockerfile",
        "strategy_app/Dockerfile",
        "market_data_dashboard/Dockerfile",
        "persistence_app/Dockerfile",
    ]
    for rel_path in dockerfiles:
        text = _read(rel_path)
        assert "COPY ml_pipeline " not in text
        assert "ml_pipeline/src" not in text


def test_live_compose_profile_does_not_mount_ml_pipeline_into_supported_services() -> None:
    compose_text = _read("docker-compose.yml")
    supported_services = [
        "ingestion_app",
        "snapshot_app",
        "persistence_app",
        "strategy_persistence_app",
        "strategy_app",
        "dashboard",
    ]
    for service in supported_services:
        pattern = rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  [A-Za-z0-9_]+:|\Z)"
        match = re.search(pattern, compose_text)
        assert match is not None, f"service block missing: {service}"
        block = match.group(1)
        assert "./ml_pipeline/artifacts:/app/ml_pipeline/artifacts:ro" not in block


def test_strategy_app_live_compose_supports_ml_pure_inputs() -> None:
    compose_text = _read("docker-compose.yml")
    env_example = _read(".env.compose.example")
    pattern = r"(?ms)^  strategy_app:\n(.*?)(?=^  [A-Za-z0-9_]+:|\Z)"
    match = re.search(pattern, compose_text)
    assert match is not None, "service block missing: strategy_app"
    block = match.group(1)

    expected_envs = [
        "ML_PURE_RUN_ID",
        "ML_PURE_MODEL_GROUP",
        "ML_PURE_MODEL_PACKAGE",
        "ML_PURE_THRESHOLD_REPORT",
    ]
    for name in expected_envs:
        assert f'{name}: "${{{name}:-}}"' in block
        assert f"{name}=" in env_example

    expected_args = [
        "--ml-pure-run-id",
        "--ml-pure-model-group",
        "--ml-pure-model-package",
        "--ml-pure-threshold-report",
    ]
    for arg in expected_args:
        assert f'"{arg}"' in block
