import json
from pathlib import Path

RUN_DIR = Path("/opt/option_trading/.run/strategy_app_sim/32fca5d4-d648-4848-a74d-9b9f0c44a792")

for fname in ["decisions.jsonl", "decision_traces.jsonl", "votes.jsonl", "session_summary.jsonl"]:
    fpath = RUN_DIR / fname
    if not fpath.exists():
        print(f"{fname}: MISSING")
        continue
    lines = [l.strip() for l in fpath.open() if l.strip()]
    print(f"\n{fname}: {len(lines)} lines")
    if lines:
        d = json.loads(lines[0])
        print(f"  keys: {list(d.keys())}")
        # look for action/blocker
        for k in ["action", "blocker", "blocking_gate", "outcome", "type", "event"]:
            if k in d:
                print(f"  {k}={d[k]!r}")
    # count actions
    actions = {}
    for l in lines:
        try:
            d = json.loads(l)
            a = d.get("action") or d.get("outcome") or d.get("type") or d.get("event") or "?"
            actions[a] = actions.get(a, 0) + 1
        except:
            pass
    print(f"  action counts: {actions}")
