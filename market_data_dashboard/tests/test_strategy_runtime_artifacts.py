from __future__ import annotations

import json
from pathlib import Path

from market_data_dashboard.runtime_artifacts import load_strategy_runtime_observability


def test_load_strategy_runtime_observability_reads_artifacts(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".run" / "strategy_app"
    runtime_dir.mkdir(parents=True)

    (runtime_dir / "runtime_config.json").write_text(
        json.dumps(
            {
                "artifact_type": "strategy_runtime_config",
                "model": {
                    "run_id": "run-123",
                    "model_group": "banknifty_futures/h15_tp_auto",
                    "block_expiry": True,
                },
                "rollout": {"stage": "capped_live"},
                "strategy_profile_id": "ml_pure_staged_v1",
            }
        ),
        encoding="utf-8",
    )
    (runtime_dir / "runtime_state.json").write_text(
        json.dumps(
            {
                "artifact_type": "strategy_runtime_state",
                "engine": "ml_pure",
                "session": {
                    "bars_evaluated": 12,
                    "entries_taken": 2,
                    "last_entry_at": "2026-03-23T10:15:00+05:30",
                    "hold_counts": {"feature_stale": 3},
                    "hold_rate": 0.75,
                },
                "risk": {
                    "is_halted": False,
                    "is_paused": True,
                    "consecutive_losses": 1,
                },
                "position": {"has_position": True},
            }
        ),
        encoding="utf-8",
    )
    (runtime_dir / "metrics.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-03-23T10:10:00+05:30", "event": "hold", "reason": "feature_stale"}),
                json.dumps({"ts": "2026-03-23T10:15:00+05:30", "event": "entry", "direction": "CE"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = load_strategy_runtime_observability(repo_root=tmp_path, metrics_tail_limit=5)

    assert payload["status"] == "healthy"
    assert payload["summary"]["run_id"] == "run-123"
    assert payload["summary"]["entries_taken"] == 2
    assert payload["summary"]["metrics_last_event"] == "entry"
    assert payload["artifacts"]["runtime_config_present"] is True
    assert payload["artifacts"]["runtime_state_present"] is True


def test_load_strategy_runtime_observability_handles_missing_artifacts(tmp_path: Path) -> None:
    payload = load_strategy_runtime_observability(repo_root=tmp_path)

    assert payload["status"] == "unavailable"
    assert payload["artifacts"]["runtime_config_present"] is False
    assert payload["artifacts"]["runtime_state_present"] is False
