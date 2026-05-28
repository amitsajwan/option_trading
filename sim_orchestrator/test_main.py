import unittest
from pathlib import Path
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
            patch.object(sim_main, "spawn_persistence", return_value="pid-2") as persist,
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
        cons.assert_called_once_with("run-1", env_overrides={})
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


    def test_compose_run_env_merges_manifest_overrides(self) -> None:
        env = sim_main._compose_run_env(
            "run-9",
            {
                "STRATEGY_PROFILE_ID": "debit_multi_v1",
                "ENTRY_ML_MIN_PROB": "0.65",
            },
        )
        self.assertEqual(env["SIM_RUN_ID"], "run-9")
        self.assertEqual(env["STRATEGY_PROFILE_ID"], "debit_multi_v1")
        self.assertEqual(env["ENTRY_ML_MIN_PROB"], "0.65")

    def test_spawn_consumer_skips_env_file_when_overrides_present(self) -> None:
        with (
            patch.object(sim_main, "_repo_root", return_value=Path("/opt/option_trading")),
            patch.object(sim_main, "_compose_files", return_value=["/opt/option_trading/docker-compose.yml"]),
            patch.object(sim_main, "_load_env_file", return_value={"STRATEGY_PROFILE_ID": "r1s_top3_paper_v1"}),
            patch.object(sim_main.Path, "is_file", return_value=True),
            patch.object(sim_main.subprocess, "run", return_value=MagicMock(stdout="cid\n")) as run,
        ):
            sim_main.spawn_consumer(
                "run-10",
                env_overrides={"STRATEGY_PROFILE_ID": "debit_multi_v1"},
            )
        cmd = run.call_args.args[0]
        self.assertNotIn("--env-file", cmd)
        self.assertIn("-e", cmd)
        self.assertIn("STRATEGY_PROFILE_ID=debit_multi_v1", cmd)


if __name__ == "__main__":
    unittest.main()
