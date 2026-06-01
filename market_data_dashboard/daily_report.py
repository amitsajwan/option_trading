"""Daily P&L report generator — run at 15:40 IST (after market close).

Reads from MongoDB strategy_positions and generates a markdown report.
Sends via Telegram if configured. Saves to docs/reports/YYYY-MM-DD.md.

Usage:
  python -m market_data_dashboard.daily_report

Or via cron on GCP VM (/etc/cron.d/trading-daily-report):
  40 10 * * 1-5 root cd /opt/option_trading && \
      .venv/bin/python -m market_data_dashboard.daily_report \
      >> .run/daily_report.log 2>&1

(15:40 IST = 10:10 UTC)

Env vars:
  MONGO_HOST, MONGO_PORT, MONGO_DB, MONGO_COLL_STRATEGY_POSITIONS
  REPORT_OUTPUT_DIR     path for .md files  (default: docs/reports)
  ALERT_ENABLED, ALERT_TELEGRAM_TOKEN, ALERT_TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

_REPORT_DIR = Path(os.getenv("REPORT_OUTPUT_DIR", "docs/reports"))
_COLL = os.getenv("MONGO_COLL_STRATEGY_POSITIONS", "strategy_positions")


def _mongo_db():
    from pymongo import MongoClient
    host = os.getenv("MONGO_HOST", "localhost")
    port = int(os.getenv("MONGO_PORT", "27017") or "27017")
    db_name = os.getenv("MONGO_DB", "trading_ai")
    client: Any = MongoClient(host=host, port=port, serverSelectionTimeoutMS=5000)
    return client[db_name]


def _fetch_today_positions(db, trade_date: str) -> list[dict]:
    coll = db[_COLL]
    # Fetch all POSITION_CLOSE events for today
    docs = list(coll.find(
        {"trade_date_ist": trade_date, "event": "POSITION_CLOSE"},
        {"_id": 0, "direction": 1, "strike": 1, "entry_premium": 1, "exit_premium": 1,
         "pnl_pct": 1, "mfe_pct": 1, "mae_pct": 1, "bars_held": 1,
         "exit_reason": 1, "exit_policy_triggered": 1,
         "market_time_ist": 1, "timestamp": 1}
    ).sort("timestamp", 1))
    return docs


def _pct(v) -> str:
    try:
        return f"{float(v):+.2%}"
    except Exception:
        return "—"


def _float(v) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def build_report(trade_date: str, positions: list[dict]) -> str:
    if not positions:
        return f"# Daily P&L Report — {trade_date}\n\nNo closed positions today.\n"

    pnls = [_float(p.get("pnl_pct")) for p in positions]
    mfes = [_float(p.get("mfe_pct")) for p in positions]
    session_pnl = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_mfe = sum(mfes) / len(mfes) if mfes else 0
    capture_ratios = [p / m for p, m in zip(pnls, mfes) if m > 0]
    avg_capture = sum(capture_ratios) / len(capture_ratios) if capture_ratios else 0

    lines = [
        f"# Daily P&L Report — {trade_date}",
        "",
        "## Summary",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Session P&L | {_pct(session_pnl)} |",
        f"| Trades | {len(positions)} |",
        f"| Win rate | {len(wins)}/{len(positions)} ({len(wins)/len(positions):.0%}) |",
        f"| Avg MFE | {_pct(avg_mfe)} |",
        f"| Avg MFE capture | {avg_capture:.0%} |",
        "",
        "## Trades",
        "| # | Time | Dir | Entry | Exit | P&L | MFE | MAE | Bars | Exit Reason |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, p in enumerate(positions, 1):
        exit_reason = str(p.get("exit_policy_triggered") or p.get("exit_reason") or "")
        lines.append(
            f"| {i} | {str(p.get('market_time_ist') or '')[:5]} "
            f"| {p.get('direction','')} "
            f"| {_float(p.get('entry_premium')):.0f} "
            f"| {_float(p.get('exit_premium')):.0f} "
            f"| {_pct(p.get('pnl_pct'))} "
            f"| {_pct(p.get('mfe_pct'))} "
            f"| {_pct(p.get('mae_pct'))} "
            f"| {int(p.get('bars_held') or 0)} "
            f"| {exit_reason} |"
        )

    lines += ["", f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST*", ""]
    return "\n".join(lines)


def main() -> int:
    today = date.today().isoformat()
    logger.info("generating daily report for %s", today)

    try:
        db = _mongo_db()
        positions = _fetch_today_positions(db, today)
        logger.info("fetched %d closed positions", len(positions))
    except Exception:
        logger.exception("failed to fetch positions from MongoDB")
        return 1

    report = build_report(today, positions)

    # Write to docs/reports/
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _REPORT_DIR / f"{today}.md"
    out_path.write_text(report, encoding="utf-8")
    logger.info("report saved to %s", out_path)

    # Send via Telegram
    try:
        from execution_app.alerts import _send_telegram
        # Telegram has 4096 char limit — send summary section only
        summary_lines = report.split("## Trades")[0].strip()
        _send_telegram(f"<b>Daily Report {today}</b>\n<pre>{summary_lines}</pre>")
    except Exception:
        logger.warning("failed to send daily report via Telegram")

    return 0


if __name__ == "__main__":
    sys.exit(main())
