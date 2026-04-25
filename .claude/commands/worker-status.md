Show the full pipeline worker status by running these commands:

1. Get worker heartbeats: `curl -s "http://localhost:8643/api/status" | uv run python -c "import sys,json,time;s=json.load(sys.stdin);now=time.time();print('WORKERS:');[print(f'  {w[\"hostname\"]:25s}  {\"STALE\" if now-w.get(\"last_seen\",0)>300 else w.get(\"status\",\"?\"):10s}  task={str(w.get(\"current_task_id\",\"-\")):6s}  heartbeat={int(now-w.get(\"last_seen\",0))}s ago') for w in s.get('workers',[])]"`

2. Get running tasks: `curl -s "http://localhost:8643/api/queue?status=running&limit=10" | uv run python -c "import sys,json;items=json.load(sys.stdin);print(f'\nRUNNING ({len(items)}):');[print(f'  {i.get(\"claimed_by\",\"?\"):25s}  #{i[\"id\"]}  {i[\"task_type\"]:12s}  {(i.get(\"game_id\") or \"?\")[:42]}') for i in items]"`

3. Get queued tasks: `curl -s "http://localhost:8643/api/queue?status=queued&limit=20" | uv run python -c "import sys,json;items=json.load(sys.stdin);print(f'\nQUEUED ({len(items)}):');[print(f'  pri={i[\"priority\"]:3d}  {i[\"task_type\"]:12s}  {(i.get(\"game_id\") or \"?\")[:42]}') for i in items]"`

Present results as a single summary. Flag any workers that are STALE (heartbeat > 300s). Flag any running tasks with no heartbeat in > 120s.
