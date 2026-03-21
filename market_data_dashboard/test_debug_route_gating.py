import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import market_data_dashboard.app as dashboard_app


class _RequestStub:
    scope = {"type": "http"}


class DebugRouteGatingTests(unittest.TestCase):
    def test_test_page_hidden_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(dashboard_app.test_page())

        self.assertEqual(ctx.exception.status_code, 404)

    def test_simple_dashboard_hidden_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(dashboard_app.simple_dashboard(_RequestStub()))

        self.assertEqual(ctx.exception.status_code, 404)

    def test_test_page_renders_when_debug_routes_enabled(self) -> None:
        with patch.dict("os.environ", {"DASHBOARD_ENABLE_DEBUG_ROUTES": "1"}, clear=False):
            response = asyncio.run(dashboard_app.test_page())

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
