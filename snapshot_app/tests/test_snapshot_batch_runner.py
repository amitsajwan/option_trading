from __future__ import annotations

import argparse

from snapshot_app.historical import snapshot_batch_runner as runner
from snapshot_app.historical.snapshot_batch_runner import _row_count_status


def test_row_count_status_treats_short_session_counts_as_ok() -> None:
    assert _row_count_status(318) == "OK"


def test_row_count_status_marks_outliers_as_error() -> None:
    assert _row_count_status(200) == "ERROR"


def test_main_validates_without_report_path(tmp_path, monkeypatch) -> None:
    calls = {"validate": 0}

    monkeypatch.setattr(
        argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            base=str(tmp_path),
            raw_root=None,
            vix_root=None,
            normalize_only=False,
            force_normalize=False,
            normalize_jobs=1,
            snapshot_jobs=1,
            slice_months=6,
            slice_warmup_days=90,
            build_stage="all",
            instrument="BANKNIFTY-I",
            year=None,
            plan_year_runs=False,
            min_day=None,
            max_day=None,
            lookback_days=30,
            no_resume=False,
            dry_run=False,
            validate_only=False,
            validate_days=5,
            log_every=10,
            write_batch_days=20,
            required_fields=None,
            required_schema_version=None,
            rebuild_missing_fields=False,
            print_iv_diagnostics=False,
            build_source="historical",
            build_run_id=None,
            validate_ml_flat_contract=False,
            manifest_out=None,
            validation_report_out=None,
            window_manifest_out=None,
            window_min_trading_days=150,
            window_max_gap_days=7,
        ),
    )
    monkeypatch.setattr(runner, "run_snapshot_builds", lambda **kwargs: {"status": "complete"})

    def _fake_validate_output(*args, **kwargs):
        calls["validate"] += 1
        return {"ok": True}

    monkeypatch.setattr(runner, "validate_output", _fake_validate_output)

    assert runner.main() == 0
    assert calls["validate"] == 1
