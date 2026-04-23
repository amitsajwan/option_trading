import sys
sys.path.insert(0, '/home/savitasajwan03/option_trading')
from pathlib import Path
from ml_pipeline_2.staged.pipeline import _load_dataset

parquet_root = Path('/home/savitasajwan03/option_trading/.data/ml_pipeline/parquet_data').resolve()
dataset_name = 'snapshots_ml_flat_v2'

print(f'Loading dataset {dataset_name} from {parquet_root}...')
try:
    df = _load_dataset(parquet_root, dataset_name)
    print(f'Loaded {len(df)} rows, {len(df.columns)} columns')
    print('SUCCESS')
except Exception as e:
    print(f'ERROR: {type(e).__name__}: {e}')
    import traceback
    traceback.print_exc()
