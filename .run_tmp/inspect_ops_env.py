import json, sys
d = json.load(sys.stdin)
print("ENTRY_PIPELINE_V2:", d.get("STRATEGY_ENTRY_PIPELINE_V2", "NOT_SET"))
print("MIN_CONFIDENCE:", d.get("STRATEGY_MIN_CONFIDENCE", "NOT_SET"))
print("CONSENSUS_BYPASS:", d.get("CONSENSUS_BYPASS_MIN_CONFIDENCE", "NOT_SET"))
