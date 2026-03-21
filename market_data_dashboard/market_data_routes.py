from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter


class DashboardMarketDataRouter:
    def __init__(
        self,
        *,
        get_redis_mongo_sync_lag_fn: Callable[..., Any],
        market_data_status_fn: Callable[..., Any],
        validate_data_availability_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
        get_ohlc_data_fn: Callable[..., Any],
        get_chart_data_fn: Callable[..., Any],
        get_technical_indicators_fn: Callable[..., Any],
        get_available_instruments_fn: Callable[..., Any],
        get_market_depth_fn: Callable[..., Any],
        get_options_chain_fn: Callable[..., Any],
    ) -> None:
        self._get_redis_mongo_sync_lag_fn = get_redis_mongo_sync_lag_fn
        self._market_data_status_fn = market_data_status_fn
        self._validate_data_availability_fn = validate_data_availability_fn
        self._get_ohlc_data_fn = get_ohlc_data_fn
        self._get_chart_data_fn = get_chart_data_fn
        self._get_technical_indicators_fn = get_technical_indicators_fn
        self._get_available_instruments_fn = get_available_instruments_fn
        self._get_market_depth_fn = get_market_depth_fn
        self._get_options_chain_fn = get_options_chain_fn

        router = APIRouter(tags=["market-data"])
        router.add_api_route("/api/market-data/sync-lag", self.get_redis_mongo_sync_lag, methods=["GET"])
        router.add_api_route("/api/market-data/status", self.market_data_status, methods=["GET"])
        router.add_api_route("/api/market-data/ohlc/{instrument}", self.get_ohlc_data, methods=["GET"])
        router.add_api_route("/api/market-data/charts/{instrument}", self.get_chart_data, methods=["GET"])
        router.add_api_route("/api/market-data/indicators/{instrument}", self.get_technical_indicators, methods=["GET"])
        router.add_api_route("/api/market-data/instruments", self.get_available_instruments, methods=["GET"])
        router.add_api_route("/api/market-data/depth/{instrument}", self.get_market_depth, methods=["GET"])
        router.add_api_route("/api/market-data/options/{instrument}", self.get_options_chain, methods=["GET"])
        self.router = router

    async def get_redis_mongo_sync_lag(self, instrument: str = "") -> Any:
        return await self._get_redis_mongo_sync_lag_fn(instrument=instrument)

    async def market_data_status(self) -> Any:
        return await self._market_data_status_fn()

    def validate_data_availability(self, status: Dict[str, Any]) -> Dict[str, Any]:
        return self._validate_data_availability_fn(status)

    async def get_ohlc_data(
        self,
        instrument: str,
        timeframe: str = "1min",
        limit: int = 100,
        order: str = "asc",
    ) -> Any:
        return await self._get_ohlc_data_fn(
            instrument=instrument,
            timeframe=timeframe,
            limit=limit,
            order=order,
        )

    async def get_chart_data(
        self,
        instrument: str,
        timeframe: str = "1min",
        limit: int = 200,
    ) -> Any:
        return await self._get_chart_data_fn(
            instrument=instrument,
            timeframe=timeframe,
            limit=limit,
        )

    async def get_technical_indicators(self, instrument: str, timeframe: str = "1min") -> Any:
        return await self._get_technical_indicators_fn(
            instrument=instrument,
            timeframe=timeframe,
        )

    async def get_available_instruments(self) -> Any:
        return await self._get_available_instruments_fn()

    async def get_market_depth(self, instrument: str) -> Any:
        return await self._get_market_depth_fn(instrument)

    async def get_options_chain(self, instrument: str, expiry: Optional[str] = None) -> Any:
        return await self._get_options_chain_fn(instrument, expiry=expiry)
