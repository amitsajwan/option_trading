from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import start_apps


def test_start_or_verify_strategy_launches_detached_process_and_polls_health() -> None:
    launched: dict[str, object] = {}
    health_calls: list[tuple[list[str], float]] = []

    def _fake_launch_detached(**kwargs):
        launched.update(kwargs)
        return {"component": "strategy_app", "pid": 4242}

    def _fake_run_json_command(cmd, timeout_seconds):
        health_calls.append((list(cmd), float(timeout_seconds)))
        return 0, {"component": "strategy_app", "status": "healthy", "process": {"running": True}}

    with (
        patch("start_apps.find_matching_python_processes", return_value=[]),
        patch("start_apps._launch_detached", side_effect=_fake_launch_detached),
        patch("start_apps._run_json_command", side_effect=_fake_run_json_command),
    ):
        code, payload = start_apps._start_or_verify_strategy(
            strategy_engine="ml_pure",
            strategy_topic="market:snapshot:v1",
            strategy_min_confidence=0.65,
            health_timeout_seconds=2.0,
        )

    run_dir = str(Path(".run/strategy_app").resolve())
    assert code == 0
    assert launched["component"] == "strategy_app"
    assert launched["run_dir"] == run_dir
    assert launched["cmd"] == [
        start_apps.sys.executable,
        "-m",
        "strategy_app.main",
        "--engine",
        "ml_pure",
        "--topic",
        "market:snapshot:v1",
        "--min-confidence",
        "0.65",
        "--run-dir",
        run_dir,
    ]
    assert health_calls == [
        (
            [
                start_apps.sys.executable,
                "-m",
                "strategy_app.health",
                "--topic",
                "market:snapshot:v1",
            ],
            5.0,
        )
    ]
    assert payload["launcher"]["pid"] == 4242
    assert payload["controls"]["logs_dir"] == run_dir


def test_start_or_verify_strategy_reuses_existing_process() -> None:
    with (
        patch("start_apps.find_matching_python_processes", return_value=[(101, "python -m strategy_app.main")]),
        patch(
            "start_apps._run_json_command",
            return_value=(0, {"component": "strategy_app", "status": "healthy", "process": {"running": True}}),
        ) as run_json,
        patch("start_apps._launch_detached") as launch_detached,
    ):
        code, payload = start_apps._start_or_verify_strategy(
            strategy_engine="ml_pure",
            strategy_topic="market:snapshot:v1",
            strategy_min_confidence=0.65,
            health_timeout_seconds=2.0,
        )

    assert code == 0
    launch_detached.assert_not_called()
    run_json.assert_called_once_with(
        [
            start_apps.sys.executable,
            "-m",
            "strategy_app.health",
            "--topic",
            "market:snapshot:v1",
        ],
        timeout_seconds=5.0,
    )
    assert payload["launcher"]["action"] == "already_running"


def test_start_or_verify_strategy_retries_until_health_check_turns_healthy() -> None:
    health_results = iter(
        [
            (1, {"component": "strategy_app", "status": "degraded", "process": {"running": True}}),
            (0, {"component": "strategy_app", "status": "healthy", "process": {"running": True}}),
        ]
    )

    with (
        patch("start_apps.find_matching_python_processes", return_value=[]),
        patch("start_apps._launch_detached", return_value={"component": "strategy_app", "pid": 4242}),
        patch("start_apps._run_json_command", side_effect=lambda *args, **kwargs: next(health_results)) as run_json,
        patch("start_apps.time.sleep"),
    ):
        code, payload = start_apps._start_or_verify_strategy(
            strategy_engine="ml_pure",
            strategy_topic="market:snapshot:v1",
            strategy_min_confidence=0.65,
            health_timeout_seconds=3.0,
        )

    assert code == 0
    assert payload["status"] == "healthy"
    assert payload["launcher"]["pid"] == 4242
    assert run_json.call_count == 2


def test_normalize_strategy_health_result_treats_missing_process_as_unhealthy() -> None:
    code, payload = start_apps._normalize_strategy_health_result(
        1,
        {
            "component": "strategy_app",
            "status": "degraded",
            "process": {"running": False},
        },
    )

    assert code == 2
    assert payload["status"] == "unhealthy"
