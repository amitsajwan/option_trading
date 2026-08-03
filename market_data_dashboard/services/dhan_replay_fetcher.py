"""Fetch a full trading day from ingestion_app (which owns Dhan credentials).

Dashboard must NEVER call Dhan directly — all market data proxies through
the ingestion_app /api/v1/historical/day/{instrument}?date=... endpoint.

For BANKNIFTY: http://ingestion_app:8004/api/v1/historical/day/BANKNIFTY?date=...
For NIFTY:     http://ingestion_app_nifty:8004/api/v1/historical/day/NIFTY?date=...
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

def ingestion_base_url(instrument: str) -> str:
    """ingestion_app base URL for an instrument (compose network names).

    Generic on purpose (2026-08-04): the old 2-entry _INGESTION_HOSTS dict
    raised for FINNIFTY despite ingestion_app_finnifty running in compose.
    Convention: primary (BANKNIFTY) container is unsuffixed `ingestion_app`,
    every other instrument gets `ingestion_app_{slug}` — same rule as
    contracts_app.sim_namespace uses for collections/topics/run-dirs.
    `INGESTION_APP_{INSTRUMENT}_URL` env overrides per instrument.
    """
    inst = str(instrument or "").strip().upper()
    env = os.getenv(f"INGESTION_APP_{inst}_URL", "").strip()
    if env:
        return env
    if inst == "BANKNIFTY":
        return "http://ingestion_app:8004"
    return f"http://ingestion_app_{inst.lower()}:8004"


class DhanHistoricalFetcher:
    """Fetch one trading day by proxying through ingestion_app.

    The ingestion_app containers own the Dhan credentials and expose
    GET /api/v1/historical/day/{instrument}?date=YYYY-MM-DD which returns
    index_bars + vix_bars + options in the same structure that
    dhan_replay_builder.build_snapshots_from_dhan_data() expects.
    """

    def __init__(self, progress_cb=None):
        self._progress_cb = progress_cb

    def _progress(self, step: str, msg: str):
        if self._progress_cb:
            self._progress_cb(step, msg)
        logger.info("%s: %s", step, msg)

    def fetch_day(self, instrument: str, trade_date: str, strikes: int = 5) -> Dict[str, Any]:
        """Call ingestion_app and return the historical day dict."""
        inst = instrument.upper()
        base_url = ingestion_base_url(inst)

        url = f"{base_url}/api/v1/historical/day/{inst}"
        params = {"date": trade_date, "strikes": strikes, "interval": "1"}

        self._progress("fetching", f"→ GET {url}?date={trade_date} (via ingestion_app)")
        logger.info("historical fetch: %s %s from %s", inst, trade_date, url)

        try:
            resp = requests.get(url, params=params, timeout=300)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(
                f"Cannot reach ingestion_app at {base_url} — is the container running? ({exc})"
            ) from exc
        except requests.exceptions.HTTPError as exc:
            raise RuntimeError(
                f"ingestion_app returned {resp.status_code}: {resp.text[:300]}"
            ) from exc

        data = resp.json()
        n_index = len(data.get("index_bars") or [])
        n_opts  = sum(
            len(sides.get("ce", [])) + len(sides.get("pe", []))
            for sides in (data.get("options") or {}).values()
        )
        self._progress("fetched", f"Got {n_index} index bars, {n_opts} option bars from ingestion_app")
        return data
