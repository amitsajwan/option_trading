import os, sys, subprocess

print("=== CODEBASE ===")
for p in ["/home/amits/bmm_run", "/opt/option_trading", "/home/amits/option_trading"]:
    if os.path.exists(p):
        print(f"EXISTS: {p}")
        try:
            top = os.listdir(p)
            print("  top:", sorted(top)[:20])
        except Exception as e:
            print("  err:", e)
    else:
        print(f"MISSING: {p}")

print("\n=== PYTHON PACKAGES ===")
try:
    import pymongo; print("pymongo:", pymongo.version)
except ImportError: print("pymongo: NOT INSTALLED")
try:
    import pyarrow; print("pyarrow:", pyarrow.__version__)
except ImportError: print("pyarrow: NOT INSTALLED")
try:
    import pandas; print("pandas:", pandas.__version__)
except ImportError: print("pandas: NOT INSTALLED")

print("\n=== SNAPSHOT_APP ===")
for p in ["/home/amits/bmm_run/snapshot_app", "/opt/option_trading/snapshot_app"]:
    print(f"  {p}: {'EXISTS' if os.path.exists(p) else 'MISSING'}")

print("\n=== MONGO ACCESSIBLE ===")
try:
    from pymongo import MongoClient
    c = MongoClient("localhost", 27017, serverSelectionTimeoutMS=3000)
    c.server_info()
    dbs = c.list_database_names()
    print("mongo local:", dbs)
    c.close()
except Exception as e:
    print("local mongo err:", e)
try:
    from pymongo import MongoClient
    c = MongoClient("option_trading-mongo-1", 27017, serverSelectionTimeoutMS=3000)
    c.server_info()
    print("mongo container: OK")
    c.close()
except Exception as e:
    print("container mongo err:", e)
