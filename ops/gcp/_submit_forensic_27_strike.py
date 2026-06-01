import json, urllib.request
body={
  "source_date":"2026-05-27",
  "source_coll":"phase1_market_snapshots",
  "label":"forensic_ml_entry_composite_strikepolicy_2026_05_27",
  "speed":30.0,
  "env_overrides":{
    "STRATEGY_PROFILE_ID":"trader_master_ml_entry_v1",
    "STRATEGY_ENGINE":"deterministic",
    "ENTRY_ML_MODEL_PATH":"/app/ml_pipeline_2/artifacts/entry_only/published/entry_only_model.joblib",
    "ENTRY_ML_MIN_PROB":"0.65",
    "ML_ENTRY_DIRECTION_MODE":"composite",
    "DEPTH_FEED_ENABLED":"1",
    "ML_ENTRY_BLOCK_CE":"0",
    "ML_ENTRY_BLOCK_PE":"0",
    "STRATEGY_STRIKE_SELECTION_POLICY":"oi_volume_ranked",
    "STRATEGY_STRIKE_MAX_OTM_STEPS":"2",
    "STRATEGY_STRIKE_MIN_OI":"10000",
    "STRATEGY_STRIKE_MIN_VOLUME":"10000"
  }
}
req=urllib.request.Request('http://127.0.0.1:8008/api/sim/runs',data=json.dumps(body).encode('utf-8'),headers={'Content-Type':'application/json'},method='POST')
with urllib.request.urlopen(req,timeout=30) as r:
 print(r.read().decode())
