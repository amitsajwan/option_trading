"""Emit monthly backtest windows for rules-pipeline matrices.

Usage:
    python -m ml_pipeline_2.scripts.rules_pipeline.build_monthly_windows \\
        --start 2020-08 --end 2024-10 \\
        --first-day 2020-08-03
"""
from __future__ import annotations

import argparse
import calendar
import json
from datetime import date


def _parse_ym(text: str) -> tuple[int, int]:
    year_s, month_s = text.strip().split("-", 1)
    return int(year_s), int(month_s)


def monthly_windows(
    *,
    start_ym: str,
    end_ym: str,
    first_calendar_day: str | None = None,
) -> list[dict[str, str]]:
    y0, m0 = _parse_ym(start_ym)
    y1, m1 = _parse_ym(end_ym)
    first_override: date | None = None
    if first_calendar_day:
        first_override = date.fromisoformat(first_calendar_day)

    windows: list[dict[str, str]] = []
    year, month = y0, m0
    while (year, month) <= (y1, m1):
        last_day = calendar.monthrange(year, month)[1]
        start = date(year, month, 1)
        end = date(year, month, last_day)
        if first_override and (year, month) == (first_override.year, first_override.month):
            start = first_override
        name = f"{year}_{month:02d}"
        windows.append(
            {
                "name": name,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        )
        month += 1
        if month > 12:
            month = 1
            year += 1
    return windows


def main() -> None:
    parser = argparse.ArgumentParser(description="Print monthly window JSON array")
    parser.add_argument("--start", default="2020-08", help="First month YYYY-MM")
    parser.add_argument("--end", default="2024-10", help="Last month YYYY-MM")
    parser.add_argument(
        "--first-day",
        default="2020-08-03",
        help="Override start of first month (corpus first trade day)",
    )
    args = parser.parse_args()
    windows = monthly_windows(
        start_ym=args.start,
        end_ym=args.end,
        first_calendar_day=args.first_day,
    )
    print(json.dumps(windows, indent=2))


if __name__ == "__main__":
    main()
