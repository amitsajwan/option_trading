import json
from pathlib import Path

RUN_DIR = Path("/opt/option_trading/.run/strategy_app_sim/32fca5d4-d648-4848-a74d-9b9f0c44a792")

# Find a trace with a real vote / confidence / ml signal
dt_lines = [l.strip() for l in (RUN_DIR / "decision_traces.jsonl").open() if l.strip()]

# Find one where candidates exist
for l in dt_lines:
    d = json.loads(l)
    cands = d.get("candidates") or []
    if cands:
        print("=== Decision Trace with candidates ===")
        print(f"  snapshot_id : {d['snapshot_id']}")
        print(f"  timestamp   : {d['timestamp']}")
        print(f"  final_outcome: {d.get('final_outcome')}")
        print(f"  primary_blocker_gate: {d.get('primary_blocker_gate')}")
        print(f"  flow_gates  : {d.get('flow_gates')}")
        print(f"  regime_context: {d.get('regime_context')}")
        print(f"  risk_state  : {d.get('risk_state')}")
        mdiag = d.get("model_diagnostics") or {}
        print(f"  model_diagnostics keys: {list(mdiag.keys())}")
        for cand in cands[:2]:
            print(f"  candidate: {cand}")
        break

# Also look at a 'no_strategy_votes' decision
dec_lines = [l.strip() for l in (RUN_DIR / "decisions.jsonl").open() if l.strip()]
for l in dec_lines:
    d = json.loads(l)
    if d.get("blocking_gate") == "confidence_gate":
        print("\n=== CONFIDENCE_GATE block sample ===")
        print(f"  snapshot_id: {d['snapshot_id']}")
        print(f"  ts: {d['ts']}")
        votes = d.get("votes") or []
        for v in votes:
            print(f"  vote: dir={v.get('direction')} conf={v.get('confidence')} grade={v.get('grade')}")
        break

# Find a sideways_returns_mixed block
for l in dec_lines:
    d = json.loads(l)
    if d.get("blocking_gate") == "sideways_returns_mixed":
        print("\n=== SIDEWAYS_RETURNS_MIXED block sample ===")
        print(f"  snapshot_id: {d['snapshot_id']}")
        votes = d.get("votes") or []
        for v in votes:
            print(f"  vote: dir={v.get('direction')} conf={v.get('confidence')} strategy={v.get('strategy')}")
        inp = d.get("input") or {}
        print(f"  regime: {inp.get('regime')} session_phase: {inp.get('session_phase')}")
        break
