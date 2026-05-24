"""Print month x rule PASS/FAIL grid from a rules-pipeline leaderboard.md."""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("leaderboard", type=Path)
    args = parser.parse_args()
    text = args.leaderboard.read_text(encoding="utf-8")
    row_re = re.compile(
        r"\|\s*\d+\s*\|\s*\*\*(PASS|FAIL)\*\*\s*\|\s*(\S+)\s*\|\s*(\d{4}_\d{2})\s*\|"
    )
    by_rule: dict[str, dict[str, str]] = defaultdict(dict)
    for status, rule, month in row_re.findall(text):
        by_rule[rule][month] = status

    months = sorted({m for d in by_rule.values() for m in d})
    rules = sorted(by_rule)
    print(f"Source: {args.leaderboard}")
    print(f"Months: {len(months)}  Rules: {len(rules)}\n")
    header = "month\t" + "\t".join(rules)
    print(header)
    for month in months:
        cols = [by_rule[r].get(month, "-") for r in rules]
        print(month + "\t" + "\t".join(cols))
    print()
    for rule in rules:
        passes = sum(1 for m in months if by_rule[rule].get(m) == "PASS")
        print(f"{rule}: {passes}/{len(months)} months PASS")


if __name__ == "__main__":
    main()
