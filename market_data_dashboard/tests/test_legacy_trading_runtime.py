from pathlib import Path
from unittest.mock import patch

import market_data_dashboard.app as dashboard_app


def test_legacy_trading_runtime_disabled_by_default_even_if_package_exists(tmp_path: Path) -> None:
    with patch.object(dashboard_app, "ML_PIPELINE_SRC", tmp_path), patch.dict("os.environ", {}, clear=True):
        tmp_path.mkdir(parents=True, exist_ok=True)
        status = dashboard_app._legacy_trading_runtime_status()
    assert status["enabled"] is False
    assert status["package_present"] is True
    assert status["requested"] is False


def test_legacy_trading_runtime_enables_only_when_env_and_package_present(tmp_path: Path) -> None:
    with patch.object(dashboard_app, "ML_PIPELINE_SRC", tmp_path), patch.dict(
        "os.environ",
        {dashboard_app.LEGACY_TRADING_RUNTIME_ENV: "1"},
        clear=True,
    ):
        tmp_path.mkdir(parents=True, exist_ok=True)
        status = dashboard_app._legacy_trading_runtime_status()
    assert status["enabled"] is True
    assert status["package_present"] is True
    assert status["requested"] is True


def test_legacy_trading_runtime_reports_missing_package_when_requested(tmp_path: Path) -> None:
    missing = tmp_path / "missing_ml_pipeline_src"
    with patch.object(dashboard_app, "ML_PIPELINE_SRC", missing), patch.dict(
        "os.environ",
        {dashboard_app.LEGACY_TRADING_RUNTIME_ENV: "1"},
        clear=True,
    ):
        status = dashboard_app._legacy_trading_runtime_status()
    assert status["enabled"] is False
    assert status["package_present"] is False
    assert status["requested"] is True
