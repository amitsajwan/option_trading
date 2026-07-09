"""ProtectiveStopManager — broker-side disaster stop for live positions.

Why this exists (2026-07-09): the strategy evaluates positions once per minute
and only while the whole stack is healthy. Between bars — and during any outage
(stale token, container restart, VM reboot) — an open option position had ZERO
protection. This module places a resting STOP_LOSS_MARKET SELL at the broker
right after every live entry fill, so the exchange itself caps the loss even if
our stack is dead.

Division of labour:
  - The app's exit stack (giveback / timestop / trailing / premium stop) stays
    the primary exit — the broker stop is set WIDER (EXEC_BROKER_STOP_PCT,
    default 25% vs the app's ~18%) so it only fires when the app can't.
  - On an app-driven exit we cancel the resting stop FIRST; selling while the
    stop is still live would risk a double-sell (naked short) if both execute.
  - If the cancel reveals the stop already TRADED (broker beat us during an
    outage), the exit is reconciled from the stop's own fill and NO market sell
    is placed.

State lives in Redis (execution:protective_stop:{position_id}) so reconciliation
survives execution_app restarts. Orders are DAY validity — they die at market
close, and the key TTL matches.

Adapters that don't implement place_protective_stop (paper/kite/shadow) return
None and this whole module degrades to a no-op.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from .adapter.base import BrokerAdapter, OrderResult

logger = logging.getLogger(__name__)

_KEY_PREFIX = "execution:protective_stop:"
_KEY_TTL_SEC = 86400  # DAY-validity order; state is meaningless tomorrow


def _enabled() -> bool:
    # Default OFF. 2026-07-09 live incident: Dhan silently converted our
    # STOP_LOSS_MARKET into an immediate priced LIMIT sell (triggerPrice dropped
    # to 0) and flattened the open position on the spot. Do NOT re-enable until
    # the resting-order type is proven to actually rest (see docs/ incident note).
    return str(os.getenv("EXEC_BROKER_STOP_ENABLED", "0") or "0").strip().lower() in {"1", "true", "yes"}


def _stop_pct() -> float:
    try:
        v = float(os.getenv("EXEC_BROKER_STOP_PCT", "0.25") or 0.25)
    except ValueError:
        v = 0.25
    # Guard against a fat-fingered env value: a stop tighter than 5% would
    # constantly front-run the app's own exits; wider than 90% is pointless.
    return min(max(v, 0.05), 0.90)


class ProtectiveStopManager:
    """Places, tracks, and reconciles the broker-side disaster stop."""

    def __init__(self, adapter: BrokerAdapter, redis_client) -> None:
        self._adapter = adapter
        self._r = redis_client

    # ── after a confirmed live entry fill ────────────────────────────────────
    def protect_entry(self, *, position_id: Optional[str], signal, fill_price: Optional[float],
                      fill_qty: Optional[int]) -> Optional[OrderResult]:
        """Place the broker-side stop for a just-filled entry. Returns the stop
        OrderResult, or None when disabled/unsupported/failed (failure is alerted,
        never raised — the entry fill must still be emitted)."""
        if not _enabled():
            return None
        place = getattr(self._adapter, "place_protective_stop", None)
        if place is None:
            return None
        if not position_id or not fill_price or fill_price <= 0 or not fill_qty or fill_qty <= 0:
            logger.warning(
                "protective_stop: cannot protect pos=%s — missing fill data (price=%s qty=%s)",
                position_id, fill_price, fill_qty,
            )
            self._alert(f"<b>UNPROTECTED POSITION</b> {position_id or '?'} — no fill data for broker stop")
            return None
        trigger = fill_price * (1.0 - _stop_pct())
        try:
            result = place(signal, qty_units=int(fill_qty), trigger_price=trigger)
        except Exception as exc:  # never let stop placement kill the fill path
            logger.exception("protective_stop: placement raised for pos=%s", position_id)
            self._alert(f"<b>UNPROTECTED POSITION</b> {position_id} — broker stop placement error: {exc}")
            return None
        if result is None:
            return None
        if result.status == "placed" and result.order_id:
            self._r.set(
                _KEY_PREFIX + str(position_id),
                json.dumps({"order_id": result.order_id, "trigger": round(trigger, 2),
                            "qty": int(fill_qty)}),
                ex=_KEY_TTL_SEC,
            )
            logger.info(
                "protective_stop: armed pos=%s order_id=%s trigger=%.2f qty=%d (entry %.2f, -%.0f%%)",
                position_id, result.order_id, trigger, fill_qty, fill_price, _stop_pct() * 100,
            )
            self._alert(
                f"<b>BROKER STOP ARMED</b> {signal.direction} {signal.strike}"
                f"  trigger {trigger:.1f} (-{_stop_pct():.0%} of {fill_price:.1f})"
            )
        else:
            logger.error(
                "protective_stop: placement REJECTED pos=%s err=%s — position runs unprotected",
                position_id, result.error,
            )
            self._alert(f"<b>UNPROTECTED POSITION</b> {position_id} — broker stop rejected: {result.error}")
        return result

    # ── before an app-driven exit ────────────────────────────────────────────
    def reconcile_before_exit(self, position_id: Optional[str]) -> Optional[OrderResult]:
        """Cancel the resting stop before the app sells to close.

        Returns None when the exit should proceed normally (stop cancelled, or
        no stop on record). Returns the stop's FILLED OrderResult when the broker
        stop already closed the position — the caller must SKIP the market sell
        and emit the exit fill from that result instead.
        """
        if not position_id:
            return None
        key = _KEY_PREFIX + str(position_id)
        raw = self._r.get(key)
        if not raw:
            return None
        try:
            state = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._r.delete(key)
            return None
        stop_order_id = str(state.get("order_id") or "")
        if not stop_order_id:
            self._r.delete(key)
            return None

        if self._adapter.cancel_order(stop_order_id):
            logger.info("protective_stop: cancelled pos=%s order_id=%s — proceeding with app exit",
                        position_id, stop_order_id)
            self._r.delete(key)
            return None

        # Cancel failed — usually because the stop already executed. Check.
        status = self._adapter.get_order_status(stop_order_id)
        if status.is_filled:
            logger.warning(
                "protective_stop: broker stop ALREADY EXECUTED pos=%s order_id=%s fill=%s "
                "— skipping app market sell (position is closed)",
                position_id, stop_order_id, status.fill_price,
            )
            self._alert(
                f"<b>BROKER STOP FIRED</b> pos {position_id}"
                f"  filled @ {status.fill_price or '?'} — app exit reconciled, no double sell"
            )
            self._r.delete(key)
            return status
        if status.status in ("cancelled", "rejected"):
            # Stop is dead anyway (e.g. expired at close) — exit proceeds.
            self._r.delete(key)
            return None

        # Ambiguous: cancel failed but the stop still looks live. Retry once,
        # then proceed with the exit regardless — a stranded position is worse
        # than the (alerted) small risk of the stop firing after we're flat.
        if self._adapter.cancel_order(stop_order_id):
            self._r.delete(key)
            return None
        logger.error(
            "protective_stop: could not cancel or confirm stop pos=%s order_id=%s status=%s "
            "— proceeding with exit; CANCEL THE STOP MANUALLY if it is still pending",
            position_id, stop_order_id, status.status,
        )
        self._alert(
            f"<b>CHECK BROKER</b> pos {position_id}: stop order {stop_order_id} could not be "
            f"cancelled (status={status.status}). Cancel manually if pending."
        )
        self._r.delete(key)
        return None

    @staticmethod
    def _alert(text: str) -> None:
        try:
            from .alerts import alert_raw
            alert_raw(text)
        except Exception:
            logger.debug("protective_stop: alert send failed", exc_info=True)
