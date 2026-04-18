import asyncio
import unittest

from fastapi import HTTPException

import market_data_dashboard.app as dashboard_app


class _FakeVelocityTestingService:
    def __init__(self) -> None:
        self.last_policy_kwargs = None
        self.last_heatmap_kwargs = None

    def test_policies_for_date_range(self, **kwargs):
        self.last_policy_kwargs = dict(kwargs)
        return {
            "summary": {"total_tests": 1},
            "statistics": {"total_snapshots_with_velocity": 1},
            "results": [],
        }

    def get_velocity_heatmap(self, **kwargs):
        self.last_heatmap_kwargs = dict(kwargs)
        return {"summary": {"snapshots_analyzed": 1}}


class VelocityTestingRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        if not hasattr(dashboard_app, "test_velocity_policies"):
            self.skipTest("velocity testing routes are unavailable")
        self._old_service = dashboard_app._velocity_testing_service
        self._fake_service = _FakeVelocityTestingService()
        dashboard_app._velocity_testing_service = self._fake_service

    def tearDown(self) -> None:
        dashboard_app._velocity_testing_service = self._old_service

    def test_velocity_testing_page_renders(self) -> None:
        request = type("RequestStub", (), {"scope": {"type": "http"}})()
        response = asyncio.run(dashboard_app.velocity_testing_page(request))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Velocity Policy Testing", response.body)

    def test_velocity_policy_endpoint_delegates_to_service(self) -> None:
        payload = asyncio.run(
            dashboard_app.test_velocity_policies(
                date_from="2026-04-11",
                date_to="2026-04-18",
                trade_direction="CE",
                min_velocity_score=0.25,
            )
        )

        self.assertEqual(payload["statistics"]["total_snapshots_with_velocity"], 1)
        self.assertEqual(self._fake_service.last_policy_kwargs["date_from"], "2026-04-11")
        self.assertEqual(self._fake_service.last_policy_kwargs["trade_direction"], "CE")
        self.assertEqual(self._fake_service.last_policy_kwargs["min_velocity_score"], 0.25)

    def test_velocity_routes_return_500_when_service_unavailable(self) -> None:
        dashboard_app._velocity_testing_service = None

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                dashboard_app.test_velocity_policies(
                    date_from="2026-04-11",
                    date_to="2026-04-18",
                )
            )

        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
