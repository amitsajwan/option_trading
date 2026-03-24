from __future__ import annotations

from typing import Any, Callable, Optional
from fastapi import APIRouter, HTTPException


class DashboardPublicContractRouter:
    def __init__(
        self,
        *,
        now_iso_ist: Callable[[], str],
        normalize_timestamp_fields: Callable[[Any], Any],
        public_topic_schemas: Callable[[], dict[str, Any]],
        public_schema_version: str,
        public_topics: list[str],
        build_runtime_catalog: Callable[..., Any],
        public_timeframes: list[str],
        load_runtime_instruments: Callable[..., Any],
        default_instrument: str,
        canonical_contract_timeframe: Callable[[str], str],
        get_system_mode: Callable[[], Any],
        market_data_api_url: str,
        requests_get: Callable[..., Any],
        get_ohlc_data: Callable[..., Any],
        get_technical_indicators: Callable[..., Any],
        get_market_depth: Callable[..., Any],
        get_options_chain: Callable[..., Any],
        get_current_mode_hint: Callable[..., Optional[str]],
    ) -> None:
        self._now_iso_ist = now_iso_ist
        self._normalize_timestamp_fields = normalize_timestamp_fields
        self._public_topic_schemas = public_topic_schemas
        self._public_schema_version = public_schema_version
        self._public_topics = list(public_topics)
        self._build_runtime_catalog = build_runtime_catalog
        self._public_timeframes = list(public_timeframes)
        self._load_runtime_instruments = load_runtime_instruments
        self._default_instrument = default_instrument
        self._canonical_contract_timeframe = canonical_contract_timeframe
        self._get_system_mode = get_system_mode
        self._market_data_api_url = market_data_api_url
        self._requests_get = requests_get
        self._get_ohlc_data = get_ohlc_data
        self._get_technical_indicators = get_technical_indicators
        self._get_market_depth = get_market_depth
        self._get_options_chain = get_options_chain
        self._get_current_mode_hint = get_current_mode_hint

        router = APIRouter(tags=["public-contract"])
        router.add_api_route("/api/schema", self.get_public_schema_index, methods=["GET"])
        router.add_api_route("/api/schema/{topic}", self.get_public_topic_schema, methods=["GET"])
        router.add_api_route("/api/capabilities", self.get_public_capabilities, methods=["GET"])
        router.add_api_route("/api/catalog", self.get_public_catalog, methods=["GET"])
        router.add_api_route("/api/examples/{topic}", self.get_public_topic_example, methods=["GET"])
        self.router = router

    def bind_market_data_handlers(
        self,
        *,
        get_ohlc_data: Optional[Callable[..., Any]] = None,
        get_technical_indicators: Optional[Callable[..., Any]] = None,
        get_market_depth: Optional[Callable[..., Any]] = None,
        get_options_chain: Optional[Callable[..., Any]] = None,
    ) -> None:
        if get_ohlc_data is not None:
            self._get_ohlc_data = get_ohlc_data
        if get_technical_indicators is not None:
            self._get_technical_indicators = get_technical_indicators
        if get_market_depth is not None:
            self._get_market_depth = get_market_depth
        if get_options_chain is not None:
            self._get_options_chain = get_options_chain

    async def get_public_schema_index(self) -> Any:
        now_iso = self._now_iso_ist()
        schemas = self._public_topic_schemas()
        topics = [
            {
                "topic": topic,
                "version": self._public_schema_version,
                "schema_url": f"/api/schema/{topic}",
                "example_url": f"/api/examples/{topic}",
            }
            for topic in schemas.keys()
            if topic in schemas
            if topic in self._public_topics
        ]
        return self._normalize_timestamp_fields(
            {
                "status": "ok",
                "schema_version": self._public_schema_version,
                "timestamp": now_iso,
                "topics": topics,
            }
        )

    async def get_public_topic_schema(self, topic: str) -> Any:
        topic_key = str(topic or "").strip().lower()
        schemas = self._public_topic_schemas()
        if topic_key not in schemas:
            raise HTTPException(status_code=404, detail=f"Unknown topic '{topic_key}'. Supported: {', '.join(self._public_topics)}")
        return self._normalize_timestamp_fields(
            {
                "status": "ok",
                "topic": topic_key,
                "schema_version": self._public_schema_version,
                "timestamp": self._now_iso_ist(),
                "schema": schemas[topic_key],
            }
        )

    async def get_public_capabilities(self, instrument: Optional[str] = None) -> Any:
        catalog = await self._build_runtime_catalog(instrument=instrument)
        selected_instrument = catalog.get("instrument")
        return self._normalize_timestamp_fields(
            {
                "status": catalog.get("status", "ok"),
                "schema_version": self._public_schema_version,
                "timestamp": self._now_iso_ist(),
                "mode": catalog.get("mode"),
                "instruments": catalog.get("instruments", []),
                "default_instrument": selected_instrument,
                "timeframes": list(self._public_timeframes),
                "topics": list(self._public_topics),
                "availability": catalog.get("availability", {}),
                "apis": catalog.get("apis", {}),
                "ws_topics": catalog.get("ws_topics", {}),
                "schema_index": "/api/schema",
            }
        )

    async def get_public_catalog(self, instrument: Optional[str] = None) -> Any:
        return await self._build_runtime_catalog(instrument=instrument)

    async def get_public_topic_example(
        self,
        topic: str,
        instrument: Optional[str] = None,
        timeframe: str = "1m",
    ) -> Any:
        topic_key = str(topic or "").strip().lower()
        if topic_key not in self._public_topics:
            raise HTTPException(status_code=404, detail=f"Unknown topic '{topic_key}'. Supported: {', '.join(self._public_topics)}")

        instruments = await self._load_runtime_instruments(max_instruments=20)
        selected_instrument = str(instrument or "").strip() or (instruments[0] if instruments else self._default_instrument)
        tf = self._canonical_contract_timeframe(timeframe)
        tf_for_endpoint = tf if tf != "1m" else "1min"

        sample: Any = None
        if topic_key != "mode" and not selected_instrument:
            sample = {"status": "no_data", "message": "No instrument available"}
        elif topic_key == "mode":
            sample = await self._get_system_mode()
        elif topic_key == "tick":
            try:
                resp = self._requests_get(
                    f"{self._market_data_api_url}/api/v1/market/tick/{selected_instrument}",
                    timeout=3,
                )
                if resp.status_code == 200:
                    sample = self._normalize_timestamp_fields(resp.json())
                else:
                    sample = {"status": "no_data", "error": f"Upstream tick API returned {resp.status_code}"}
            except Exception as exc:
                sample = {"status": "error", "error": str(exc)}
        elif topic_key == "ohlc":
            sample = await self._get_ohlc_data(
                instrument=selected_instrument,
                timeframe=tf_for_endpoint,
                limit=3,
                order="desc",
            )
        elif topic_key == "indicators":
            sample = await self._get_technical_indicators(
                instrument=selected_instrument,
                timeframe=tf_for_endpoint,
            )
        elif topic_key == "depth":
            sample = await self._get_market_depth(selected_instrument)
        elif topic_key == "options":
            sample = await self._get_options_chain(selected_instrument)
        elif topic_key == "strategy_eval":
            sample = {
                "event_type": "run_progress",
                "run_id": "example-run-id",
                "timestamp": self._now_iso_ist(),
                "progress_pct": 42.5,
                "current_day": "2024-01-15",
                "total_days": 20,
                "message": "Replay in progress",
                "error": None,
            }

        return self._normalize_timestamp_fields(
            {
                "status": "ok",
                "topic": topic_key,
                "schema_version": self._public_schema_version,
                "timestamp": self._now_iso_ist(),
                "mode": self._get_current_mode_hint(timeout_seconds=1.0) or "unknown",
                "instrument": selected_instrument,
                "timeframe": tf,
                "sample": sample,
            }
        )
