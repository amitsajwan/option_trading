"""
Velocity Testing Routes - Expose velocity policy testing via HTTP API.

Provides endpoints for:
- Testing velocity-enhanced policies on date ranges
- Visualizing velocity metrics heatmaps
- Comparing policy decisions with/without velocity
"""

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates


class DashboardVelocityTestingRouter:
    """Routes for velocity policy testing and visualization."""

    def __init__(
        self,
        *,
        templates: Jinja2Templates,
        test_policies_fn: Callable[..., Any],
        get_heatmap_fn: Callable[..., Any],
        velocity_testing_available: Callable[[], bool],
    ) -> None:
        self._templates = templates
        self._test_policies_fn = test_policies_fn
        self._get_heatmap_fn = get_heatmap_fn
        self._velocity_testing_available = velocity_testing_available

        router = APIRouter(tags=["velocity_testing"])
        router.add_api_route(
            "/trading/velocity-testing",
            self.velocity_testing_page,
            methods=["GET"],
            response_class=HTMLResponse,
        )
        router.add_api_route(
            "/api/trading/velocity-testing/test",
            self.test_velocity_policies,
            methods=["GET"],
        )
        router.add_api_route(
            "/api/trading/velocity-testing/heatmap",
            self.get_velocity_heatmap,
            methods=["GET"],
        )
        self.router = router

    def _require_velocity_testing_service(self) -> None:
        if not bool(self._velocity_testing_available()):
            raise HTTPException(
                status_code=500,
                detail="velocity testing service unavailable - ensure data provider is configured",
            )

    async def velocity_testing_page(self, request: Request) -> RedirectResponse:
        """Redirect to modern /app UI."""
        return RedirectResponse(url="/app?tab=velocity", status_code=302)

    async def test_velocity_policies(
        self,
        date_from: str,
        date_to: str,
        trade_direction: Optional[str] = None,
        min_velocity_score: float = 0.0,
    ) -> Any:
        """
        Test velocity-enhanced policies over a date range.
        
        Args:
            date_from: Start date (YYYY-MM-DD)
            date_to: End date (YYYY-MM-DD)
            trade_direction: Optional filter "CE" or "PE"
            min_velocity_score: Minimum regime confidence to include
            
        Returns:
            Dictionary with test results, statistics, and sample entries
        """
        self._require_velocity_testing_service()
        try:
            return self._test_policies_fn(
                date_from=date_from,
                date_to=date_to,
                trade_direction=trade_direction,
                min_velocity_score=min_velocity_score,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Testing failed: {str(exc)}")

    async def get_velocity_heatmap(
        self,
        date_from: str,
        date_to: str,
    ) -> Any:
        """
        Get velocity metrics heatmap for date range.
        
        Shows distribution of velocity signals to understand conditions
        that favor entries vs blocks.
        """
        self._require_velocity_testing_service()
        try:
            return self._get_heatmap_fn(
                date_from=date_from,
                date_to=date_to,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Heatmap generation failed: {str(exc)}")
