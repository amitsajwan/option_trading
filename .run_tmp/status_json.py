import json
from pathlib import Path
p = Path('/tmp/status.json')
if p.exists():
    d = json.loads(p.read_text())
    print('status:', d.get('status'))
    print('counts:', d.get('metadata',{}).get('collection_counts',{}))
else:
    print('no status file')
