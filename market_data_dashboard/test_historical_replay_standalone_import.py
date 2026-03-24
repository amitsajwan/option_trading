from __future__ import annotations

import importlib
import sys
from pathlib import Path


def test_historical_replay_monitor_imports_in_script_mode() -> None:
    dashboard_dir = Path("market_data_dashboard").resolve()
    added = False
    if str(dashboard_dir) not in sys.path:
        sys.path.insert(0, str(dashboard_dir))
        added = True
    try:
        module = importlib.import_module("historical_replay_monitor_service")
        assert getattr(module, "HistoricalReplayMonitorService", None) is not None
    finally:
        if added:
            sys.path.remove(str(dashboard_dir))
        sys.modules.pop("historical_replay_monitor_service", None)
        sys.modules.pop("historical_replay_repository", None)
