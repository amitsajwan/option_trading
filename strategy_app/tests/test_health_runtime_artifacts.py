from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from strategy_app.engines.runtime_artifacts import RuntimeArtifactStore
from strategy_app.health import evaluate


class StrategyHealthRuntimeArtifactsTests(unittest.TestCase):
    def test_health_exposes_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = RuntimeArtifactStore(root / "artifacts")
            store.write_config(
                {
                    "artifact_type": "strategy_runtime_config",
                    "strategy_profile_id": "ml_pure_staged_v1",
                    "model": {
                        "run_id": "run-1",
                        "model_group": "banknifty_futures/h15_tp_auto",
                    },
                    "rollout": {
                        "stage": "paper",
                    },
                    "ml_pure": {
                        "block_expiry": False,
                    },
                }
            )
            store.write_state(
                {
                    "artifact_type": "strategy_runtime_state",
                    "session": {
                        "bars_evaluated": 2,
                        "entries_taken": 1,
                        "last_entry_at": "2026-03-02T09:31:00+05:30",
                        "hold_counts": {
                            "entry_below_threshold": 1,
                        },
                        "hold_rate": 0.5,
                    },
                    "risk": {
                        "is_halted": False,
                        "is_paused": False,
                        "session_pnl_pct": 0.0,
                        "consecutive_losses": 0,
                    },
                    "position": {
                        "has_position": True,
                        "current": {
                            "position_id": "pos-1",
                        },
                    },
                }
            )
            store.append_metric({"event": "session_start", "ts": "2026-03-02T09:15:00+05:30"})
            store.append_metric({"event": "entry", "ts": "2026-03-02T09:31:00+05:30"})

            redis_client = MagicMock()
            redis_client.ping.return_value = True

            with patch("strategy_app.health.find_matching_python_processes", return_value=[("1234", "strategy_app.main")]), patch("strategy_app.health.redis.Redis", return_value=redis_client):
                result, code = evaluate(topic="market:snapshot:v1", artifact_dir=str(store.paths.root), metrics_tail_lines=5)

            self.assertEqual(code, 0)
            self.assertEqual(result["runtime_artifacts"]["status"], "healthy")
            self.assertEqual(result["runtime_artifacts"]["summary"]["run_id"], "run-1")
            self.assertEqual(result["runtime_artifacts"]["summary"]["metrics_latest_event"], "entry")
            self.assertTrue(result["runtime_artifacts"]["summary"]["has_position"])


if __name__ == "__main__":
    unittest.main()
