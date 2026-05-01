import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from fastapi.templating import Jinja2Templates

from market_data_dashboard.operator_routes import DashboardOperatorRouter


def _router() -> DashboardOperatorRouter:
    return DashboardOperatorRouter(
        templates=Jinja2Templates(directory=str(Path("."))),
        templates_dir=Path("."),
        market_data_api_url="http://market-data",
        redis_host="localhost",
        redis_port=6379,
        get_live_strategy_monitor_service=lambda: None,
        get_strategy_eval_service=lambda: None,
        normalize_timestamp_fields=lambda value: value,
        now_iso_ist=lambda: "2026-05-01T12:00:00+05:30",
    )


class OperatorHaltRouteTests(TestCase):
    def test_halt_routes_create_report_and_clear_shared_sentinel(self) -> None:
        with TemporaryDirectory() as tmp_dir, mock.patch.dict("os.environ", {"STRATEGY_RUN_DIR": tmp_dir}, clear=False):
            router = _router()
            halt_path = Path(tmp_dir).resolve() / "operator_halt"

            initial = asyncio.run(router.get_operator_halt())
            self.assertFalse(initial["halted"])
            self.assertEqual(Path(initial["path"]), halt_path)

            posted = asyncio.run(router.post_operator_halt())
            self.assertTrue(posted["halted"])
            self.assertTrue(halt_path.exists())

            cleared = asyncio.run(router.delete_operator_halt())
            self.assertFalse(cleared["halted"])
            self.assertFalse(halt_path.exists())
