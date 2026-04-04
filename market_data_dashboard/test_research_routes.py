import asyncio
import unittest
from unittest.mock import patch

import market_data_dashboard.app as dashboard_app


class ResearchRouteTests(unittest.TestCase):
    def test_research_page_alias_renders(self) -> None:
        request = type("RequestStub", (), {"scope": {"type": "http"}})()
        with patch.object(dashboard_app, "evaluate_recovery_scenario", lambda **kwargs: {}), patch.object(
            dashboard_app,
            "list_recovery_scenarios",
            lambda **kwargs: {"status": "ok", "count": 0, "scenarios": []},
        ):
            response = asyncio.run(dashboard_app.trading_research_page(request))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Research", response.body)
        self.assertIn(b"Research Decision Guide", response.body)
        self.assertIn(b"Awaiting Evaluation", response.body)
        self.assertIn(b"Mode Rail", response.body)
        self.assertIn(b"Live Monitor", response.body)
        self.assertIn(b"Evaluation Compare", response.body)

    def test_research_scenarios_alias_delegates(self) -> None:
        captured = {}
        with patch.object(
            dashboard_app,
            "list_recovery_scenarios",
            lambda **kwargs: captured.update(kwargs) or {"status": "ok", "count": 1, "scenarios": [{"scenario_key": "run_a"}]},
        ), patch.object(dashboard_app, "evaluate_recovery_scenario", lambda **kwargs: {}):
            payload = asyncio.run(dashboard_app.get_trading_research_scenarios())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["scenarios"][0]["scenario_key"], "run_a")
        self.assertIn("roots", captured)
        self.assertTrue(captured["roots"])

    def test_research_evaluation_alias_forwards_params(self) -> None:
        captured = {}

        def _fake_eval(**kwargs):
            captured.update(kwargs)
            return {"status": "ok", "summary": {"rows_total": 5}}

        with patch.object(dashboard_app, "evaluate_recovery_scenario", _fake_eval), patch.object(
            dashboard_app,
            "list_recovery_scenarios",
            lambda **kwargs: {"status": "ok", "count": 0, "scenarios": []},
        ):
            payload = asyncio.run(
                dashboard_app.get_trading_research_evaluation(
                    scenario_key="scenario_1",
                    date_from="2024-01-10",
                    date_to="2024-01-20",
                    recipe_id="recipe_a",
                    threshold=0.61,
                )
            )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["summary"]["rows_total"], 5)
        self.assertEqual(captured["scenario_key"], "scenario_1")
        self.assertEqual(captured["date_from"], "2024-01-10")
        self.assertEqual(captured["date_to"], "2024-01-20")
        self.assertEqual(captured["recipe_id"], "recipe_a")
        self.assertEqual(captured["threshold"], 0.61)
        self.assertIn("roots", captured)
        self.assertTrue(captured["roots"])


if __name__ == "__main__":
    unittest.main()
