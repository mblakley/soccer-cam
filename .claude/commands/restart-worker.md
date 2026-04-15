Restart a pipeline worker. Parse `$ARGUMENTS` to identify the target machine.

## Machine Lookup

| Alias | Hostname | IP |
|-------|----------|----|
| laptop | jared-laptop | 192.168.86.24 |
| fortnite | FORTNITE-OP | 192.168.86.250 |

If `$ARGUMENTS` matches an alias or hostname (case-insensitive), use that machine's IP.
If `$ARGUMENTS` is empty or unrecognized, ask the user which machine.

## Procedure (follow EXACTLY — do NOT deviate)

Credentials: user=`training`, pass=`amy4ever`

### Step 1: Kill python and trigger scheduled task (single PS remoting call)
```powershell
$cred = New-Object System.Management.Automation.PSCredential('training', (ConvertTo-SecureString 'amy4ever' -AsPlainText -Force))
Invoke-Command -ComputerName {IP} -Credential $cred -ScriptBlock {
    Stop-Process -Name python -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5
    schtasks /run /tn "PipelineWorker"
}
```

### Step 2: Wait 30 seconds
Do NOT check anything. The SMB session needs time to establish.
```bash
sleep 30
```

### Step 3: Verify
```powershell
$cred = New-Object System.Management.Automation.PSCredential('training', (ConvertTo-SecureString 'amy4ever' -AsPlainText -Force))
Invoke-Command -ComputerName {IP} -Credential $cred -ScriptBlock {
    Get-Process python* | Select-Object Id, StartTime | Format-Table
    Get-Content 'C:\soccer-cam-label\logs\worker.log' -Tail 5
}
```

Check that a python process is running and worker.log shows "Worker starting" with a recent timestamp.

### Step 4: Confirm task pickup via API
```bash
curl -s "http://localhost:8643/api/status" | uv run python -c "import sys,json,time;s=json.load(sys.stdin);now=time.time();[print(f'{w[\"hostname\"]}: status={w.get(\"status\")} task={w.get(\"current_task_id\")} heartbeat={int(now-w.get(\"last_seen\",0))}s ago') for w in s.get('workers',[]) if '{HOSTNAME}'.lower() in w['hostname'].lower()]"
```

## Rules — NEVER break these

- **NEVER** kill and restart more than once. If step 3 fails, tell the user the machine needs an interactive RDP login.
- **NEVER** try `net use`, `cmdkey`, or manual SMB mounting from PS remoting. These corrupt the SMB session.
- **NEVER** start python manually via PS remoting. Always use `schtasks /run /tn "PipelineWorker"`.
- **NEVER** use `Start-ScheduledTask` PowerShell cmdlet. Use `schtasks /run` instead.

## If Verification Fails

Tell the user: "The worker didn't start. RDP into {IP} as user `training` (password: `amy4ever`), open a command prompt, and run: `C:\soccer-cam-label\run_worker.bat`"
