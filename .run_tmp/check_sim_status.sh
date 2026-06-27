#!/bin/bash
set -e
curl -s http://localhost:8008/api/sim/runs | python3 -c "
import json, sys
d = json.load(sys.stdin)
rows = d.get('rows', [])
print(f'Total runs in registry: {len(rows)}')
for r in rows[:15]:
    rid = r['run_id'][:8]
    date = r['source_date']
    status = r['status']
    term = r.get('terminal_status', '?')
    print(f'  {rid} {date} status={status} term={term}')
"
