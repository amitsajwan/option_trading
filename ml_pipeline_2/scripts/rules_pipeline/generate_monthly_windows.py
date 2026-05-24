"""Emit monthly backtest windows for rules-pipeline matrices.

Default span matches corpus: 2020-08-03 through 2024-10-31.
"""
from __future__ import annotations

import argparse
import calendar
import json
from datetime import date
from pathlib import Path


def month_windows(
    *,
    start: date,
    end: date,
) -> list[dict[str, str]]:
    windows: list[dict[str, str]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        last_day = calendar.monthrange(year, month)[1]
        month_start = date(year, month, 1)
        month_end = date(year, month, last_day)
        win_start = max(month_start, start)
        win_end = min(month_end, end)
        if win_start <= win_end:
            windows.append(
                {
                    "name": f"{year}_{month:02d}",
                    "start": win_start.isoformat(),
                    "end": win_end.isoformat(),
                }
            )
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return windows


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate monthly window list JSON")
    parser.add_argument("--start", default="2020-08-03")
    parser.add_argument("--end", default="2024-10-31")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write windows array to this JSON file",
    )
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    windows = month_windows(start=start, end=end)
    payload = windows
    text = json.dumps(payload, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {len(windows)} windows to {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
