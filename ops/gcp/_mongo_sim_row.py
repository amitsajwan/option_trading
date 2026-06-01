#!/usr/bin/env python3
import sys
from pymongo import MongoClient

rid = sys.argv[1]
c = MongoClient("mongo", 27017)["trading_ai"]["strategy_eval_runs"]
row = c.find_one({"run_id": rid}, {"_id": 0, "env_overrides": 1, "label": 1})
print(row)
