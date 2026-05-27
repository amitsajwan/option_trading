import unittest
from unittest.mock import MagicMock, patch

from sim_orchestrator import main as sim_main


class SimOrchestratorTests(unittest.TestCase):
    def test_process_start_spawns_and_updates(self) -> None:
        coll = MagicMock()
        coll.find_one.return_value = {
            "run_id": "run-1",
            "source_date": "2026-05-27",
            "source_coll": "phase1_market_snapshots",
            "label": "t",
            "speed": 30.0,
            "env_overrides": {},
        }
        with (
            patch.object(sim_main, "spawn_publisher", return_value=111) as pub,
            patch.object(sim_main, "spawn_consumer", return_value="cid-1") as cons,
            patch.object(sim_main, "resolve_image_digest", return_value="sha256:abc"),
            patch.object(sim_main.threading, "Thread") as thread_cls,
        ):
            sim_main.process_command(
                coll,
                {
                    "event_type": sim_main.EVENT_START,
                    "run_id": "run-1",
                    "source_date": "2026-05-27",
                },
            )
        pub.assert_called_once()
        cons.assert_called_once_with("run-1")
        coll.update_one.assert_called()
        thread_cls.assert_called_once()

    def test_process_cancel_stops_processes(self) -> None:
        coll = MagicMock()
        coll.find_one.return_value = {
            "run_id": "run-2",
            "publisher_pid": 222,
            "consumer_container_id": "cid-2",
        }
        with (
            patch.object(sim_main.os, "kill") as kill,
            patch.object(sim_main, "stop_consumer") as stop,
        ):
            sim_main.process_command(
                coll,
                {"event_type": sim_main.EVENT_CANCEL, "run_id": "run-2"},
            )
        kill.assert_called_once()
        stop.assert_called_once_with("cid-2")


if __name__ == "__main__":
    unittest.main()
