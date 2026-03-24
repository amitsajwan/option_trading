import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from ops.gcp.operator_preflight import validate_operator_preflight


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")


def _write_guard(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "approved_for_runtime": True,
                "offline_strict_positive_passed": True,
                "paper_days_observed": 10,
                "shadow_days_observed": 10,
            }
        ),
        encoding="utf-8",
    )


def _write_manifest(path: Path, *, repo_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "kind": "gcp_runtime_release_manifest_v1",
                "release_status": "published",
                "run_id": "run_20260324_120000",
                "model_group": "banknifty_futures/h15_tp_auto",
                "app_image_tag": "20260324-a1b2c3d",
                "runtime_guard_path": "guards/runtime_guard.json",
                "threshold_report": "reports/thresholds.json",
                "training_summary": "reports/training.json",
                "runtime_env_path": "release/current_ml_pure_runtime.env",
            }
        ),
        encoding="utf-8",
    )


class OperatorPreflightTests(unittest.TestCase):
    def test_live_preflight_blocks_when_manifest_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            env_file = repo_root / ".env.compose"
            guard_path = repo_root / "guards" / "runtime_guard.json"
            threshold_report = repo_root / "reports" / "thresholds.json"
            training_report = repo_root / "reports" / "training.json"
            credentials_path = repo_root / "ingestion_app" / "credentials.json"
            credentials_path.parent.mkdir(parents=True, exist_ok=True)
            credentials_path.write_text(json.dumps({"api_key": "key", "access_token": "token"}), encoding="utf-8")
            _write_guard(guard_path)
            threshold_report.parent.mkdir(parents=True, exist_ok=True)
            threshold_report.write_text("{}", encoding="utf-8")
            training_report.write_text("{}", encoding="utf-8")
            _write_env(
                env_file,
                {
                    "GHCR_IMAGE_PREFIX": "ghcr.io/amitsajwan",
                    "APP_IMAGE_TAG": "20260324-a1b2c3d",
                    "STRATEGY_ENGINE": "ml_pure",
                    "STRATEGY_ROLLOUT_STAGE": "capped_live",
                    "STRATEGY_POSITION_SIZE_MULTIPLIER": "0.25",
                    "STRATEGY_ML_RUNTIME_GUARD_FILE": "guards/runtime_guard.json",
                    "ML_PURE_RUN_ID": "run_20260324_120000",
                    "ML_PURE_MODEL_GROUP": "banknifty_futures/h15_tp_auto",
                    "ML_PURE_THRESHOLD_REPORT": "reports/thresholds.json",
                    "ML_PURE_TRAINING_SUMMARY_PATH": "reports/training.json",
                },
            )

            with self.assertRaisesRegex(Exception, "missing"):
                validate_operator_preflight(
                    mode="live",
                    repo_root=repo_root,
                    env_file=env_file,
                    release_manifest_path=repo_root / "missing_manifest.json",
                    ghcr_image_prefix="ghcr.io/amitsajwan",
                    credentials_path=credentials_path,
                )

    @mock.patch("ops.gcp.operator_preflight._check_ghcr_images", return_value=(True, ["strategy_app"], []))
    @mock.patch("ops.gcp.operator_preflight.shutil_which", return_value="docker")
    def test_live_preflight_blocks_without_kite_credentials(self, _which: mock.Mock, _images: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            env_file = repo_root / ".env.compose"
            manifest_path = repo_root / "runtime_release_manifest.json"
            guard_path = repo_root / "guards" / "runtime_guard.json"
            threshold_report = repo_root / "reports" / "thresholds.json"
            training_report = repo_root / "reports" / "training.json"
            _write_guard(guard_path)
            threshold_report.parent.mkdir(parents=True, exist_ok=True)
            threshold_report.write_text("{}", encoding="utf-8")
            training_report.write_text("{}", encoding="utf-8")
            _write_manifest(manifest_path, repo_root=repo_root)
            _write_env(
                env_file,
                {
                    "GHCR_IMAGE_PREFIX": "ghcr.io/amitsajwan",
                    "APP_IMAGE_TAG": "20260324-a1b2c3d",
                    "STRATEGY_ENGINE": "ml_pure",
                    "STRATEGY_ROLLOUT_STAGE": "capped_live",
                    "STRATEGY_POSITION_SIZE_MULTIPLIER": "0.25",
                    "STRATEGY_ML_RUNTIME_GUARD_FILE": "guards/runtime_guard.json",
                    "ML_PURE_RUN_ID": "run_20260324_120000",
                    "ML_PURE_MODEL_GROUP": "banknifty_futures/h15_tp_auto",
                    "ML_PURE_THRESHOLD_REPORT": "reports/thresholds.json",
                    "ML_PURE_TRAINING_SUMMARY_PATH": "reports/training.json",
                },
            )

            result = validate_operator_preflight(
                mode="live",
                repo_root=repo_root,
                env_file=env_file,
                release_manifest_path=manifest_path,
                ghcr_image_prefix="ghcr.io/amitsajwan",
                credentials_path=repo_root / "ingestion_app" / "credentials.json",
            )

            self.assertEqual(result.status, "blocked")
            self.assertIn("kite credentials missing", result.blockers[0])

    def test_historical_preflight_blocks_when_requested_date_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            env_file = repo_root / ".env.compose"
            parquet_base = repo_root / ".data" / "ml_pipeline" / "parquet_data"
            snapshots_root = parquet_base / "snapshots" / "year=2026" / "chunk=test"
            snapshots_root.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {"trade_date": "2026-03-24", "timestamp": "2026-03-24T09:15:00", "schema_version": "3.0"},
                ]
            ).to_parquet(snapshots_root / "data.parquet", index=False)
            _write_env(
                env_file,
                {
                    "LIVE_TOPIC": "market:snapshot:v1",
                    "HISTORICAL_TOPIC": "market:snapshot:v1:historical",
                    "MONGO_COLL_SNAPSHOTS": "snapshots_live",
                    "MONGO_COLL_SNAPSHOTS_HISTORICAL": "snapshots_hist",
                    "MONGO_COLL_STRATEGY_VOTES": "votes_live",
                    "MONGO_COLL_STRATEGY_VOTES_HISTORICAL": "votes_hist",
                    "MONGO_COLL_TRADE_SIGNALS": "signals_live",
                    "MONGO_COLL_TRADE_SIGNALS_HISTORICAL": "signals_hist",
                    "MONGO_COLL_STRATEGY_POSITIONS": "positions_live",
                    "MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL": "positions_hist",
                },
            )

            result = validate_operator_preflight(
                mode="historical",
                repo_root=repo_root,
                env_file=env_file,
                snapshot_parquet_bucket_url="gs://snapshot-data/parquet_data",
                start_date="2026-03-25",
                end_date="2026-03-25",
                parquet_base=parquet_base,
            )

            self.assertEqual(result.status, "blocked")
            self.assertIn("historical replay date missing from parquet", result.blockers[0])

    @mock.patch.dict("sys.modules", {"pandas": None}, clear=False)
    def test_historical_preflight_uses_coverage_report_when_parquet_store_deps_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            env_file = repo_root / ".env.compose"
            parquet_base = repo_root / ".data" / "ml_pipeline" / "parquet_data"
            reports_root = parquet_base / "reports"
            reports_root.mkdir(parents=True, exist_ok=True)
            (reports_root / "coverage_audit.json").write_text(
                json.dumps(
                    {
                        "built_days": {"min": "2024-01-01", "max": "2024-10-31"},
                        "buildable_missing_count": 0,
                        "source_missing_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            _write_env(
                env_file,
                {
                    "LIVE_TOPIC": "market:snapshot:v1",
                    "HISTORICAL_TOPIC": "market:snapshot:v1:historical",
                    "MONGO_COLL_SNAPSHOTS": "snapshots_live",
                    "MONGO_COLL_SNAPSHOTS_HISTORICAL": "snapshots_hist",
                    "MONGO_COLL_STRATEGY_VOTES": "votes_live",
                    "MONGO_COLL_STRATEGY_VOTES_HISTORICAL": "votes_hist",
                    "MONGO_COLL_TRADE_SIGNALS": "signals_live",
                    "MONGO_COLL_TRADE_SIGNALS_HISTORICAL": "signals_hist",
                    "MONGO_COLL_STRATEGY_POSITIONS": "positions_live",
                    "MONGO_COLL_STRATEGY_POSITIONS_HISTORICAL": "positions_hist",
                },
            )

            result = validate_operator_preflight(
                mode="historical",
                repo_root=repo_root,
                env_file=env_file,
                snapshot_parquet_bucket_url="gs://snapshot-data/parquet_data",
                start_date="2024-10-31",
                end_date="2024-10-31",
                parquet_base=parquet_base,
            )

            self.assertEqual(result.status, "ready")
            self.assertIn("historical replay dates covered by parquet reports", result.checks)


if __name__ == "__main__":
    unittest.main()
