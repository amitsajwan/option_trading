"""TradeAlertSender — fire Telegram (or log-only) notifications on key events.

Events fired:
  POSITION_OPEN   → "BUY PE 54200 @ 1122.60  |  session: 1 trade"
  POSITION_CLOSE  → "CLOSED PE 54200 +1.69%  |  TRAILING_STOP  |  MFE 4.15%"
  HALT            → "HALTED  |  consecutive_losses=3"

Env vars:
  ALERT_ENABLED              0|1      (default 0 — silent until configured)
  ALERT_TELEGRAM_TOKEN       bot token from @BotFather
  ALERT_TELEGRAM_CHAT_ID     your chat / group / channel id
  ALERT_TELEGRAM_TIMEOUT     seconds  (default 5)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_ENABLED = str(os.getenv("ALERT_ENABLED", "0") or "0").strip() not in {"0", "false", "no", ""}
_TOKEN = str(os.getenv("ALERT_TELEGRAM_TOKEN", "") or "").strip()
_CHAT_ID = str(os.getenv("ALERT_TELEGRAM_CHAT_ID", "") or "").strip()
_TIMEOUT = float(os.getenv("ALERT_TELEGRAM_TIMEOUT", "5") or "5")


def _send_telegram(text: str) -> None:
    if not _TOKEN or not _CHAT_ID:
        logger.warning("alert: ALERT_TELEGRAM_TOKEN or ALERT_TELEGRAM_CHAT_ID not set — skipping")
        return
    try:
        import urllib.request
        import urllib.parse
        import json

        url = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                logger.warning("telegram alert failed: %s", result)
    except Exception:
        logger.exception("alert: failed to send telegram message")


class TradeAlertSender:
    """Send alerts on trade lifecycle events."""

    def on_position_open(
        self,
        *,
        direction: str,
        strike: int,
        entry_premium: float,
        lots: int,
        session_trade_count: int,
        regime: str = "",
    ) -> None:
        if not _ENABLED:
            return
        regime_tag = f"  [{regime}]" if regime else ""
        text = (
            f"<b>BUY {direction} {strike}</b>  @  {entry_premium:.0f}"
            f"  |  {lots} lot{'s' if lots != 1 else ''}"
            f"{regime_tag}"
            f"\nSession trade #{session_trade_count}"
        )
        logger.info("alert: %s", text.replace("\n", " "))
        _send_telegram(text)

    def on_position_close(
        self,
        *,
        direction: str,
        strike: int,
        pnl_pct: float,
        exit_reason: str,
        mfe_pct: float,
        mae_pct: float,
        bars_held: int,
        exit_policy_triggered: str = "",
    ) -> None:
        if not _ENABLED:
            return
        sign = "+" if pnl_pct >= 0 else ""
        policy_tag = f"  [{exit_policy_triggered}]" if exit_policy_triggered else ""
        text = (
            f"<b>CLOSED {direction} {strike}</b>  {sign}{pnl_pct:.2%}"
            f"  |  {exit_reason}{policy_tag}"
            f"\nMFE {mfe_pct:.2%}  MAE {mae_pct:.2%}  Bars {bars_held}"
        )
        logger.info("alert: %s", text.replace("\n", " "))
        _send_telegram(text)

    def on_halt(
        self,
        *,
        halt_reason: str,
        consecutive_losses: int = 0,
        session_pnl_pct: float = 0.0,
    ) -> None:
        if not _ENABLED:
            return
        text = (
            f"<b>HALTED</b>  |  {halt_reason}"
            f"\nconsecutive_losses={consecutive_losses}"
            f"  session_pnl={session_pnl_pct:.2%}"
        )
        logger.warning("alert: %s", text.replace("\n", " "))
        _send_telegram(text)

    def on_fill_rejected(
        self,
        *,
        signal_id: str,
        signal_type: str,
        error: Optional[str],
    ) -> None:
        if not _ENABLED:
            return
        text = f"<b>ORDER REJECTED</b>  {signal_type}  |  {error or 'unknown reason'}  (id: {signal_id})"
        logger.error("alert: %s", text)
        _send_telegram(text)


# Module-level singleton — import and use directly
_sender = TradeAlertSender()


def alert_open(*, direction, strike, entry_premium, lots, session_trade_count, regime=""):
    _sender.on_position_open(
        direction=direction, strike=strike, entry_premium=entry_premium,
        lots=lots, session_trade_count=session_trade_count, regime=regime,
    )


def alert_close(*, direction, strike, pnl_pct, exit_reason, mfe_pct, mae_pct, bars_held, exit_policy_triggered=""):
    _sender.on_position_close(
        direction=direction, strike=strike, pnl_pct=pnl_pct, exit_reason=exit_reason,
        mfe_pct=mfe_pct, mae_pct=mae_pct, bars_held=bars_held,
        exit_policy_triggered=exit_policy_triggered,
    )


def alert_halt(*, halt_reason, consecutive_losses=0, session_pnl_pct=0.0):
    _sender.on_halt(
        halt_reason=halt_reason, consecutive_losses=consecutive_losses,
        session_pnl_pct=session_pnl_pct,
    )


def alert_rejected(*, signal_id, signal_type, error=None):
    _sender.on_fill_rejected(signal_id=signal_id, signal_type=signal_type, error=error)
