from __future__ import annotations

import importlib
import sys
from pathlib import Path


def test_historical_replay_monitor_imports_as_package() -> None:
    module = importlib.import_module("market_data_dashboard.services.historical_replay_monitor_service")
    assert getattr(module, "HistoricalReplayMonitorService", None) is not None
