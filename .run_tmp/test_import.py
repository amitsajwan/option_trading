import sys
sys.path.insert(0, '/home/amits/bmm_run')
sys.path.insert(0, '/opt/option_trading')
try:
    from snapshot_app.historical.snapshot_batch import _flatten_snapshot, _project_rows_to_ml_flat
    print("OK: _flatten_snapshot and _project_rows_to_ml_flat imported")
except Exception as e:
    print(f"FAIL: {e}")
    import traceback; traceback.print_exc()
