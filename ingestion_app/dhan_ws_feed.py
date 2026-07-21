"""Dhan WebSocket live feed — real-time tick publisher.

Replaces the REST-poll get_tick() path for futures + VIX. Publishes to the SAME
Redis keys that the Kite path used (I2 interface contract) so snapshot_app reads
unchanged.

Architecture (2026-07-21: consolidated to ONE shared connection):
  Previously each per-instrument container (ingestion_app, ingestion_app_nifty)
  opened its OWN dhanhq.MarketFeed session on the SAME Dhan account/token —
  found live to be in a permanent HTTP 429 reconnect-fail loop all day
  (account-wide REST/quote rate contention bleeding into the WS handshake,
  not a hard per-account connection cap -- Dhan documents 5 concurrent
  sessions). This also hid a real latent bug: the old single-instrument
  constructor always resolved the futures leg via a HARDCODED "BANKNIFTY"
  argument regardless of which instrument the calling container served, so
  the NIFTY container's futures leg was silently BankNifty's contract, not
  NIFTY's -- never reached production only because the connection never
  succeeded.

  Now: ONE process (whichever container holds contracts_app.PRIMARY_INSTRUMENT,
  see dhan_data_service.py) owns the connection and subscribes to an explicit
  list of legs covering every known instrument (build_shared_legs()) plus
  VIX. Other containers construct a legs=[] DhanWsFeed purely to read the
  shared cache via is_healthy()/get_cached_tick() -- those methods only
  touch Redis, not the WS connection, so a non-owner instance never calls
  start() and needs no legs.

  DhanWsFeed (this module) — background thread running dhanhq.MarketFeed
    → on each tick: writes websocket:tick:{INSTR}:latest  (JSON)
    → health flag:  dhan:ws:feed:heartbeat              (epoch, TTL 15s)

  DhanDataService.get_tick() — detects WS cache hit, returns it (no REST call).
    Falls back to REST poll if the cache is stale/absent (heartbeat expired).

The option chain is NOT streamed via WS (Dhan WS doesn't carry it). REST poll
in get_option_chain() is unchanged.

ENV VARS
--------
DHAN_ACCESS_TOKEN       required
DHAN_CLIENT_ID          required
DHAN_WS_ENABLED         set to "0" to force REST-only mode (default: "1")
INSTRUMENT_SYMBOL       BankNifty's live futures trading symbol, e.g. BANKNIFTY26JULFUT
NIFTY_INSTRUMENT_SYMBOL NIFTY's live futures trading symbol, e.g. NIFTY26JULFUT
  (both read only by build_shared_legs(), on the owner container)

Usage (from DhanDataService.__init__):
    legs = build_shared_legs(scrip_master) if is_owner else None
    feed = DhanWsFeed(client_id, token, redis_client, legs=legs)
    if is_owner:
        feed.start()
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("dhan_ws_feed")

IST = timezone(timedelta(hours=5, minutes=30))

# Redis key shapes (I2 interface contract — must match Kite path exactly)
_TICK_KEY   = "websocket:tick:{instrument}:latest"
_HB_KEY     = "dhan:ws:feed:heartbeat"
_HB_TTL_S   = 15          # seconds; if heartbeat older than this, feed is stale

# Dhan segment constants (MarketFeed.IDX_I / NSE_FNO numeric codes)
_SEG_IDX    = 0            # IDX_I (index segment)
_SEG_FNO    = 2            # NSE_FNO

_SID_VIX    = "21"

# Subscription mode: Quote (17) carries LTP + OI + bid/ask; Ticker (15) LTP only
_MODE_QUOTE  = 17
_MODE_TICKER = 15

# Which env var holds each known instrument's live futures trading symbol
# (used as the Redis tick label for its futures leg) — matches
# docker-compose*.yml's INSTRUMENT_SYMBOL / NIFTY_INSTRUMENT_SYMBOL.
_FUTURES_LABEL_ENV = {
    "BANKNIFTY": "INSTRUMENT_SYMBOL",
    "NIFTY": "NIFTY_INSTRUMENT_SYMBOL",
}


def _now_ist() -> datetime:
    return datetime.now(tz=IST)


def _iso_ist() -> str:
    return _now_ist().isoformat()


def build_shared_legs(scrip_master: Any) -> List[Dict[str, Any]]:
    """Index + nearest-futures legs for every known instrument, plus VIX once.

    Called only by the WS-feed owner. `scrip_master` is a loaded ScripMaster
    (ingestion_app.dhan_client) — used to resolve each underlying's current
    nearest-expiry futures security id.
    """
    from contracts_app import get_instrument, known_instruments

    legs: List[Dict[str, Any]] = [
        {"segment": _SEG_IDX, "security_id": _SID_VIX, "mode": _MODE_TICKER, "label": "INDIAVIX"},
    ]
    for name in known_instruments():
        spec = get_instrument(name)
        legs.append({
            "segment": _SEG_IDX, "security_id": spec.index_security_id,
            "mode": _MODE_QUOTE, "label": name,
        })
        row = scrip_master.find_nearest_futures(name) if scrip_master is not None else None
        fut_sid = str(row.get("SEM_SMST_SECURITY_ID") or "").strip() if row else ""
        env_name = _FUTURES_LABEL_ENV.get(name)
        fut_label = str(os.getenv(env_name) or "").strip().upper() if env_name else ""
        if fut_sid and fut_label:
            legs.append({
                "segment": _SEG_FNO, "security_id": fut_sid,
                "mode": _MODE_QUOTE, "label": fut_label,
            })
        else:
            log.warning(
                "build_shared_legs: skipping %s futures leg (sid=%r label=%r) — "
                "scrip master lookup or %s env var missing",
                name, fut_sid, fut_label, env_name,
            )
    return legs


class DhanWsFeed:
    """
    Background WS feed using dhanhq.MarketFeed.

    Publishes real-time ticks for an explicit list of legs to Redis.
    DhanDataService.get_tick() reads from there instead of REST.
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        redis_client: Any,
        legs: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._client_id  = client_id
        self._token      = access_token
        self._redis      = redis_client
        self._legs       = list(legs or [])
        self._sid_label: Dict[tuple, str] = {
            (str(leg["segment"]), str(leg["security_id"])): leg["label"] for leg in self._legs
        }
        self._thread: Optional[threading.Thread] = None
        self._stop       = threading.Event()
        self._feed: Any  = None                # dhanhq.MarketFeed instance
        self._lock       = threading.Lock()

    @staticmethod
    def should_enable() -> bool:
        return str(os.getenv("DHAN_WS_ENABLED", "1")).strip() != "0"

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the WS feed in a background daemon thread.

        No-op (with a log warning) if this instance has no legs — that's the
        expected shape for a non-owner container, which only ever calls
        is_healthy()/get_cached_tick() to read the owner's published ticks.
        """
        if not self._legs:
            log.warning("DhanWsFeed.start() called with no legs — not starting a connection")
            return
        t = threading.Thread(target=self._run_loop, name="dhan-ws-feed", daemon=True)
        t.start()
        self._thread = t
        log.info("DhanWsFeed background thread started (legs=%d)", len(self._legs))

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._feed is not None:
                try:
                    self._feed.close_connection()
                except Exception:
                    pass

    def is_healthy(self) -> bool:
        """True if a heartbeat was written to Redis within the last _HB_TTL_S seconds."""
        try:
            raw = self._redis.get(_HB_KEY)
            if raw is None:
                return False
            return (time.time() - float(raw)) < _HB_TTL_S
        except Exception:
            return False

    def get_cached_tick(self, redis_key: str) -> Optional[Dict[str, Any]]:
        """Read a tick from Redis cache. Returns None if absent or stale."""
        if not self.is_healthy():
            return None
        try:
            raw = self._redis.get(redis_key)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return None

    def update_token(self, new_token: str) -> None:
        """Update the access token (called by DhanDataService token-renewal loop)."""
        self._token = new_token

    # ── internal ──────────────────────────────────────────────────────────────

    def _instruments(self) -> List[tuple]:
        return [(leg["segment"], leg["security_id"], leg["mode"]) for leg in self._legs]

    def _run_loop(self) -> None:
        """Outer reconnect loop — restarts on any fatal error with back-off."""
        backoff = 5
        while not self._stop.is_set():
            try:
                self._connect_and_run()
                backoff = 5          # reset on clean exit
            except Exception as exc:
                log.error("DhanWsFeed crashed: %s — reconnecting in %ds", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def _connect_and_run(self) -> None:
        try:
            from dhanhq import DhanContext, MarketFeed
        except ImportError:
            log.error("dhanhq package not installed — pip install 'dhanhq>=2.0,<3'")
            self._stop.wait(60)
            return

        ctx  = DhanContext(self._client_id, self._token)
        instruments = self._instruments()

        log.info("DhanWsFeed connecting (instruments=%d)...", len(instruments))

        feed = MarketFeed(
            ctx,
            instruments,
            version="v2",
            on_message=self._on_message,
            on_connect=self._on_connect,
            on_close=self._on_close,
            on_error=self._on_error,
        )
        with self._lock:
            self._feed = feed

        feed.run_forever()          # blocks; returns when WS closes or error

        with self._lock:
            self._feed = None

    # ── WS callbacks ─────────────────────────────────────────────────────────

    def _on_connect(self, _instance: Any) -> None:
        log.info("DhanWsFeed connected to Dhan WebSocket")

    def _on_close(self, _instance: Any) -> None:
        log.warning("DhanWsFeed: WebSocket closed")

    def _on_error(self, _instance: Any, error: Any) -> None:
        log.error("DhanWsFeed error: %s", error)

    def _on_message(self, _instance: Any, message: Any) -> None:
        """
        Parse a Dhan MarketFeed message and publish to Redis.

        Dhan Quote packet shape (v2):
          {
            "type": "Quote_Data",
            "exchange_segment": 0,          # IDX_I
            "security_id": 25,
            "LTP": 58400.5,
            "LTQ": 0,
            "avg_price": ...,
            "volume": 12345,
            "OI": 0,
            "top_seller": [{"bid_quantity": ..., "bid_price": ...}],
            "top_buyer":  [...],
            ...
          }
        Ticker packet has only LTP + security_id.
        """
        try:
            if not isinstance(message, dict):
                return
            sid = str(message.get("security_id") or "").strip()
            seg = message.get("exchange_segment")
            instrument_label = self._sid_to_label(sid, seg)
            if instrument_label is None:
                log.debug("DhanWsFeed: message for unregistered leg sid=%s seg=%s", sid, seg)
                return

            ltp = self._flt(message.get("LTP") or message.get("last_price"))
            oi  = self._int(message.get("OI")  or message.get("oi"))
            vol = self._int(message.get("volume"))

            bid = ask = None
            sellers = message.get("top_seller") or []
            buyers  = message.get("top_buyer")  or []
            if sellers and isinstance(sellers[0], dict):
                bid = self._flt(sellers[0].get("bid_price"))
            if buyers and isinstance(buyers[0], dict):
                ask = self._flt(buyers[0].get("ask_price"))
            mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None

            tick = {
                "instrument":   instrument_label,
                "timestamp":    _iso_ist(),
                "last_price":   ltp,
                "best_bid":     bid,
                "best_ask":     ask,
                "mid":          mid,
                "volume":       vol,
                "oi":           oi,
                "source":       "dhan_ws",
            }
            safe_key = instrument_label.replace(" ", "")
            redis_key = _TICK_KEY.format(instrument=safe_key)
            pipe = self._redis.pipeline()
            pipe.set(redis_key, json.dumps(tick, default=str))
            pipe.set(_HB_KEY, str(time.time()), ex=_HB_TTL_S * 4)
            pipe.execute()
        except Exception as exc:
            log.debug("DhanWsFeed message parse error: %s | msg=%s", exc, str(message)[:200])

    def _sid_to_label(self, sid: str, seg: Any) -> Optional[str]:
        """Map security-id + segment back to the instrument label this leg was registered under."""
        return self._sid_label.get((str(seg), str(sid)))

    @staticmethod
    def _flt(v: Any) -> Optional[float]:
        try:
            f = float(v)
            import math
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int(v: Any) -> Optional[int]:
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None
