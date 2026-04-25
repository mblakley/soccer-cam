Show pipeline queue items grouped by status. Run these commands and present results as a clear summary:

1. Running tasks:
```bash
curl -s "http://localhost:8643/api/queue?status=running&limit=20" | uv run python -c "
import sys,json,time
items=json.load(sys.stdin);now=time.time()
print(f'RUNNING ({len(items)}):')
for i in items:
    hb=int(now-i.get('heartbeat_at',0)) if i.get('heartbeat_at') else '?'
    print(f'  #{i[\"id\"]:5d}  {i[\"task_type\"]:12s}  {i.get(\"claimed_by\",\"?\"):22s}  hb={hb}s  {(i.get(\"game_id\") or \"?\")[:40]}')
"
```

2. Queued tasks:
```bash
curl -s "http://localhost:8643/api/queue?status=queued&limit=30" | uv run python -c "
import sys,json
items=json.load(sys.stdin)
print(f'\nQUEUED ({len(items)}):')
for i in items:
    target=i.get('target_machine') or 'any'
    print(f'  #{i[\"id\"]:5d}  pri={i[\"priority\"]:3d}  {i[\"task_type\"]:12s}  target={target:15s}  {(i.get(\"game_id\") or \"?\")[:40]}')
"
```

3. Recently completed (last 10):
```bash
curl -s "http://localhost:8643/api/queue?status=done&limit=10" | uv run python -c "
import sys,json,time
items=json.load(sys.stdin);now=time.time()
print(f'\nDONE (last {len(items)}):')
for i in items:
    ago=int((now-i.get('completed_at',0))/60) if i.get('completed_at') else '?'
    print(f'  #{i[\"id\"]:5d}  {i[\"task_type\"]:12s}  {i.get(\"claimed_by\",\"?\"):22s}  {ago}m ago  {(i.get(\"game_id\") or \"?\")[:40]}')
"
```

4. Recently failed (last 10):
```bash
curl -s "http://localhost:8643/api/queue?status=failed&limit=10" | uv run python -c "
import sys,json,time
items=json.load(sys.stdin);now=time.time()
print(f'\nFAILED ({len(items)}):')
for i in items:
    ago=int((now-i.get('completed_at',0))/60) if i.get('completed_at') else '?'
    err=(i.get('error') or '')[:60]
    print(f'  #{i[\"id\"]:5d}  {i[\"task_type\"]:12s}  {ago}m ago  {(i.get(\"game_id\") or \"?\")[:30]}  err={err}')
"
```

Present all four sections. Flag any running tasks with heartbeat > 120s. Flag any failed tasks with attempts >= max_attempts.
