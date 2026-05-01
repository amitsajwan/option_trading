from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import redis
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse


class DashboardDebugRouter:
    def __init__(
        self,
        *,
        base_dir: Path,
        require_debug_routes_enabled: Callable[[], None],
        redis_host: str,
        redis_port: int,
        default_instrument: str,
        logger: Any,
    ) -> None:
        self._base_dir = base_dir
        self._require_debug_routes_enabled = require_debug_routes_enabled
        self._redis_host = redis_host
        self._redis_port = int(redis_port)
        self._default_instrument = default_instrument
        self._logger = logger

        router = APIRouter(tags=["debug"])
        router.add_api_route("/test", self.test_page, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/test/redis", self.test_redis, methods=["GET"])
        router.add_api_route("/test/ltp/{instrument}", self.test_ltp, methods=["GET"])
        router.add_api_route("/test/ohlc/{instrument}", self.test_ohlc, methods=["GET"])
        router.add_api_route("/simple", self.simple_dashboard, methods=["GET"], response_class=HTMLResponse)
        router.add_api_route("/api/simple/ohlc/{instrument}", self.simple_ohlc, methods=["GET"])
        router.add_api_route("/api/simple/ltp/{instrument}", self.simple_ltp, methods=["GET"])
        router.add_api_route("/api/simple/redis-stats", self.simple_redis_stats, methods=["GET"])
        self.router = router

    async def test_page(self) -> HTMLResponse:
        self._require_debug_routes_enabled()
        test_page_path = self._base_dir / "test_page.html"
        if not test_page_path.exists():
            return HTMLResponse("<!doctype html><title>Debug Test Page</title><h1>Debug routes enabled</h1>")
        return HTMLResponse(test_page_path.read_text())

    async def test_redis(self) -> dict[str, Any]:
        self._require_debug_routes_enabled()
        try:
            client = redis.Redis(host=self._redis_host, port=self._redis_port, db=0, decode_responses=True)
            client.ping()
            instrument_pattern = self._default_instrument or "*"
            keys = client.keys(f"*{instrument_pattern}*")
            return {
                "connected": True,
                "host": self._redis_host,
                "port": self._redis_port,
                "total_keys": len(keys),
                "sample_keys": keys[:10] if keys else [],
            }
        except Exception as exc:
            return {"connected": False, "error": str(exc)}

    async def test_ltp(self, instrument: str) -> dict[str, Any]:
        self._require_debug_routes_enabled()
        try:
            client = redis.Redis(host=self._redis_host, port=self._redis_port, db=0, decode_responses=True)
            ltp_raw = client.get(f"ltp:{instrument}")
            if ltp_raw:
                return json.loads(ltp_raw)
            return {"error": "No LTP data found"}
        except Exception as exc:
            return {"error": str(exc)}

    async def test_ohlc(self, instrument: str) -> dict[str, Any]:
        self._require_debug_routes_enabled()
        try:
            client = redis.Redis(host=self._redis_host, port=self._redis_port, db=0, decode_responses=True)
            keys_to_try = [
                f"live:ohlc_sorted:{instrument}:5min",
                f"ohlc_sorted:{instrument}:5min",
                f"live:ohlc_sorted:{instrument}:5m",
            ]
            for key in keys_to_try:
                entries = client.zrange(key, -5, -1)
                if entries:
                    bars = [json.loads(entry) for entry in entries]
                    return {"key": key, "count": len(bars), "bars": bars}
            return {"error": "No OHLC data found", "tried_keys": keys_to_try}
        except Exception as exc:
            return {"error": str(exc)}

    async def simple_dashboard(self, request: Request) -> HTMLResponse:
        self._require_debug_routes_enabled()
        html_path = self._base_dir / "simple.html"
        with open(html_path, "r") as handle:
            content = handle.read()
        return HTMLResponse(content=content)

    def simple_ohlc(self, instrument: str) -> JSONResponse:
        self._require_debug_routes_enabled()
        try:
            client = redis.Redis(host=self._redis_host, port=self._redis_port, db=0, decode_responses=True)
            patterns = [
                f"live:ohlc_sorted:{instrument}:1m",
                f"ohlc_sorted:{instrument}:1m",
                f"historical:ohlc_sorted:{instrument}:1m",
                f"paper:ohlc_sorted:{instrument}:1m",
            ]
            for key in patterns:
                try:
                    results = client.zrange(key, -50, -1)
                    if results:
                        bars = []
                        for json_data in results:
                            try:
                                bars.append(json.loads(json_data))
                            except Exception:
                                continue
                        if bars:
                            return JSONResponse(content=bars)
                except Exception as exc:
                    self._logger.warning("Failed to read %s: %s", key, exc)
                    continue
            return JSONResponse(content=[])
        except Exception as exc:
            self._logger.error("Simple OHLC error: %s", exc)
            return JSONResponse(content={"error": str(exc)}, status_code=500)

    async def simple_ltp(self, instrument: str) -> JSONResponse:
        self._require_debug_routes_enabled()
        try:
            client = redis.Redis(host=self._redis_host, port=self._redis_port, db=0, decode_responses=True)
            for key in [f"ltp:{instrument}", f"live:ltp:{instrument}"]:
                try:
                    data = client.get(key)
                    if data:
                        return JSONResponse(content=json.loads(data))
                except Exception:
                    continue
            return JSONResponse(content={})
        except Exception as exc:
            self._logger.error("Simple LTP error: %s", exc)
            return JSONResponse(content={"error": str(exc)}, status_code=500)

    async def simple_redis_stats(self) -> JSONResponse:
        self._require_debug_routes_enabled()
        try:
            client = redis.Redis(host=self._redis_host, port=self._redis_port, db=0, decode_responses=True)
            total_keys = client.dbsize()
            ohlc_keys = len(list(client.scan_iter(match="*ohlc*", count=1000)))
            return JSONResponse(
                content={
                    "connected": True,
                    "total_keys": total_keys,
                    "ohlc_keys": ohlc_keys,
                    "server": f"{self._redis_host}:{self._redis_port}",
                }
            )
        except Exception as exc:
            self._logger.error("Redis stats error: %s", exc)
            return JSONResponse(content={"connected": False, "error": str(exc)}, status_code=500)
