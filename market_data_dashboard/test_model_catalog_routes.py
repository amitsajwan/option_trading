import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import market_data_dashboard.app as dashboard_app


class ModelCatalogRouteTests(unittest.TestCase):
    def test_get_trading_models_alias_reports_counts(self) -> None:
        fake_models = [
            {"instance_key": "model_a", "ready_to_run": True, "catalog_kind": "standard"},
            {"instance_key": "model_b", "ready_to_run": False, "catalog_kind": "recovery"},
            {"instance_key": "model_c", "ready_to_run": True, "catalog_kind": "recovery"},
        ]
        with patch.object(dashboard_app, "_build_trading_model_catalog", return_value=fake_models), patch.object(
            dashboard_app,
            "_legacy_trading_runtime_status",
            return_value={"enabled": False, "detail": "disabled"},
        ):
            payload = asyncio.run(dashboard_app.get_trading_models())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["ready_count"], 2)
        self.assertEqual(payload["research_count"], 2)
        self.assertFalse(payload["legacy_trading_runtime"]["enabled"])

    def test_trading_terminal_model_redirects_to_prefill_url(self) -> None:
        fake_models = [
            {
                "instance_key": "alpha_model",
                "prefill_url": "/trading?model=alpha_model&model_package=artifacts/model.joblib",
            }
        ]
        with patch.object(dashboard_app, "_build_trading_model_catalog", return_value=fake_models):
            response = asyncio.run(dashboard_app.trading_terminal_model("Alpha Model"))

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/trading?model=alpha_model&model_package=artifacts/model.joblib")

    def test_get_trading_model_evaluation_alias_sets_ok_status(self) -> None:
        fake_snapshot = {"summary": {"best_model": "logreg"}}
        with patch.object(
            dashboard_app,
            "_build_model_eval_snapshot",
            return_value=fake_snapshot,
        ) as build_snapshot:
            payload = asyncio.run(
                dashboard_app.get_trading_model_evaluation(
                    summary_path="reports/summary.json",
                    training_report_path="reports/training.json",
                    policy_report_path="reports/policy.json",
                )
            )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["summary"]["best_model"], "logreg")
        args = build_snapshot.call_args[0]
        self.assertTrue(str(args[0]).endswith(str(Path("reports/summary.json"))))
        self.assertTrue(str(args[1]).endswith(str(Path("reports/training.json"))))
        self.assertTrue(str(args[2]).endswith(str(Path("reports/policy.json"))))


if __name__ == "__main__":
    unittest.main()
