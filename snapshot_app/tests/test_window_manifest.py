import pytest

from snapshot_app.historical.window_manifest import (
    split_boundaries_for_days,
    validate_window_manifest,
    window_manifest_hash,
)


def test_window_manifest_hash_is_stable_for_same_payload() -> None:
    payload = {
        "window_start": "2026-01-01",
        "window_end": "2026-07-31",
        "trading_days": 150,
        "all_days_required_schema": True,
        "schema_version": "3.0",
        "generated_at": "2026-03-17T00:00:00Z",
        "source_path": "C:/data",
    }
    assert window_manifest_hash(payload) == window_manifest_hash(dict(payload))


def test_validate_window_manifest_marks_formal_ready() -> None:
    payload = {
        "window_start": "2026-01-01",
        "window_end": "2026-07-31",
        "trading_days": 150,
        "all_days_required_schema": True,
        "schema_version": "3.0",
        "generated_at": "2026-03-17T00:00:00Z",
        "source_path": "C:/data",
    }
    validated = validate_window_manifest(payload, formal_run=True)
    assert validated["formal_ready"] is True
    assert validated["exploratory_only"] is False


def test_split_boundaries_for_days_builds_non_empty_60_20_20_segments() -> None:
    days = [f"2026-01-{day:02d}" for day in range(1, 11)]
    split = split_boundaries_for_days(days)
    assert split == {
        "train_start": "2026-01-01",
        "train_end": "2026-01-06",
        "valid_start": "2026-01-07",
        "valid_end": "2026-01-08",
        "eval_start": "2026-01-09",
        "eval_end": "2026-01-10",
    }


def test_split_boundaries_for_days_requires_at_least_three_days() -> None:
    with pytest.raises(ValueError, match="at least 3 trading days"):
        split_boundaries_for_days(["2026-01-01", "2026-01-02"])
