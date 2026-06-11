"""See the session-brief prompt and (if a key is set) the live grounded reply.

  docker exec -e GROUNDING_ENABLED=1 -e GEMINI_WEB_API_KEY=... dashboard \
      python /app/strategy_app/tools/session_brief_probe.py
Prints the exact context + prompt sent to Gemini, then the structured brief.
Without a key it prints the prompt only (so you can review the wording)."""
import json
import os
import sys

sys.path.insert(0, "/app")
from pymongo import MongoClient

from strategy_app.market.snapshot_accessor import SnapshotAccessor
from strategy_app.brain.session_bias import build_context
from strategy_app.brain.oversight.gemini_web import _brief_prompt, fetch_session_brief

DAY = os.getenv("PROMPT_DAY", "2026-06-10")
col = MongoClient("mongo", 27017)["trading_ai"]["phase1_market_snapshots"]
docs = list(col.find({"trade_date_ist": DAY}).sort("timestamp", 1))
snaps = [x for x in ((d.get("payload") or {}).get("snapshot") for d in docs) if x]
# use an early bar (~5 min after open) — the morning brief moment
acc = SnapshotAccessor(snaps[min(5, len(snaps) - 1)])
ctx = build_context(acc)

print("=== CONTEXT (our levels sent to Gemini) ===")
print(json.dumps(ctx, indent=2, default=str))
print("\n=== PROMPT ===")
print(_brief_prompt(ctx))

key = (os.getenv("GEMINI_WEB_API_KEY", "") or os.getenv("BRAIN_LLM_API_KEY", "")).strip()
if not key:
    print("\n[no GEMINI_WEB_API_KEY set -> prompt-only preview; set key to call live grounding]")
else:
    print("\n=== LIVE GROUNDED BRIEF ===")
    print(json.dumps(fetch_session_brief(ctx, api_key=key), indent=2, default=str))
