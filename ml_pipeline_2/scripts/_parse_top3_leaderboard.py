"""One-off: parse r1s_top3 leaderboard PASS rows by rule."""
import re
import sys
from collections import defaultdict
from pathlib import Path

lb_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "ml_pipeline_2/artifacts/rules_runs/r1s_top3_20260520/leaderboard.md"
)
text = lb_path.read_text(encoding="utf-8")
pass_re = re.compile(r"\|\s*\d+\s*\|\s*\*\*PASS\*\*\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|")
rows = pass_re.findall(text)
by_rule: dict[str, list[str]] = defaultdict(list)
for rule, window in rows:
    by_rule[rule].append(window)

targets = [
    "R1S_TOP3_S3_COMPOSITE",
    "R1S_UNLIMITED_CONTROL",
    "R1S_TOP3_S0_FIRST",
    "R1S_TOP3_S1_RET5M",
]
print(f"Source: {lb_path}")
print(f"Total PASS rows in leaderboard: {len(rows)}\n")
for rule in targets:
    wins = sorted(by_rule.get(rule, []))
    q = [w for w in wins if re.match(r"20\d\d_q", w)]
    extra = [w for w in wins if w not in q]
    print(f"{rule}")
    print(f"  quarterly PASS: {len(q)}/17 — {', '.join(q) or '(none)'}")
    print(f"  other PASS: {len(extra)} — {', '.join(extra) or '(none)'}")
    print(f"  total PASS windows: {len(wins)}")
    print()
