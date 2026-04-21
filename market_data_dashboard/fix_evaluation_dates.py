#!/usr/bin/env python3
"""Fix evaluation run dates from 2026 to 2024 in MongoDB."""

import os
import sys
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from pymongo import MongoClient, UpdateOne
    from pymongo.errors import BulkWriteError
except ImportError:
    print("pymongo not installed. Install with: pip install pymongo")
    sys.exit(1)


def get_mongo_client():
    """Get MongoDB client from environment or default."""
    mongo_uri = os.getenv("MONGO_URI", "mongodb://mongo:27017")
    return MongoClient(mongo_uri)


def fix_evaluation_dates():
    """Fix dates in evaluation collections - subtract 2 years from 2026 dates."""
    client = get_mongo_client()
    db = client[os.getenv("MONGO_DB", "trading_ai")]
    
    collections_to_fix = [
        "strategy_votes_historical",
        "trade_signals_historical",
        "strategy_positions_historical",
        "strategy_decision_traces_historical",
    ]
    
    total_updated = 0
    
    for coll_name in collections_to_fix:
        if coll_name not in db.list_collection_names():
            print(f"Collection {coll_name} not found, skipping...")
            continue
            
        collection = db[coll_name]
        bulk_ops = []
        
        # Find all documents with trade_date_ist containing 2026
        cursor = collection.find({
            "trade_date_ist": {"$regex": "^2026-"}
        })
        
        for doc in cursor:
            updates = {}
            
            # Fix trade_date_ist (primary date field for raw collections)
            if doc.get("trade_date_ist") and str(doc["trade_date_ist"]).startswith("2026-"):
                new_date = str(doc["trade_date_ist"]).replace("2026-", "2024-", 1)
                updates["trade_date_ist"] = new_date
                
            if updates:
                bulk_ops.append(
                    UpdateOne(
                        {"_id": doc["_id"]},
                        {"$set": updates}
                    )
                )
                
                if len(bulk_ops) >= 1000:
                    try:
                        result = collection.bulk_write(bulk_ops)
                        total_updated += result.modified_count
                        print(f"Updated {result.modified_count} documents in {coll_name}")
                    except BulkWriteError as e:
                        print(f"Bulk write error in {coll_name}: {e.details}")
                    bulk_ops = []
        
        # Write remaining operations
        if bulk_ops:
            try:
                result = collection.bulk_write(bulk_ops)
                total_updated += result.modified_count
                print(f"Updated {result.modified_count} documents in {coll_name}")
            except BulkWriteError as e:
                print(f"Bulk write error in {coll_name}: {e.details}")
    
    # Also fix the runs collection
    runs_coll_name = str(os.getenv("MONGO_COLL_STRATEGY_EVAL_RUNS") or "strategy_eval_runs")
    if runs_coll_name in db.list_collection_names():
        runs_coll = db[runs_coll_name]
        bulk_ops = []
        
        cursor = runs_coll.find({
            "$or": [
                {"date_from": {"$regex": "^2026-"}},
                {"date_to": {"$regex": "^2026-"}},
            ]
        })
        
        for doc in cursor:
            updates = {}
            
            if doc.get("date_from") and str(doc["date_from"]).startswith("2026-"):
                updates["date_from"] = str(doc["date_from"]).replace("2026-", "2024-", 1)
                
            if doc.get("date_to") and str(doc["date_to"]).startswith("2026-"):
                updates["date_to"] = str(doc["date_to"]).replace("2026-", "2024-", 1)
                
            if updates:
                bulk_ops.append(
                    UpdateOne(
                        {"_id": doc["_id"]},
                        {"$set": updates}
                    )
                )
                
                if len(bulk_ops) >= 1000:
                    try:
                        result = runs_coll.bulk_write(bulk_ops)
                        total_updated += result.modified_count
                        print(f"Updated {result.modified_count} documents in strategy_evaluation_runs")
                    except BulkWriteError as e:
                        print(f"Bulk write error in runs: {e.details}")
                    bulk_ops = []
        
        if bulk_ops:
            try:
                result = runs_coll.bulk_write(bulk_ops)
                total_updated += result.modified_count
                print(f"Updated {result.modified_count} documents in strategy_evaluation_runs")
            except BulkWriteError as e:
                print(f"Bulk write error in runs: {e.details}")
    
    client.close()
    print(f"\nTotal documents updated: {total_updated}")
    return total_updated


if __name__ == "__main__":
    print("Fixing evaluation dates from 2026 to 2024...")
    fix_evaluation_dates()
    print("Done!")
