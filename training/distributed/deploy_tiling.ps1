# Deploy tiling scripts to laptop and start remote tiling
# Run from server: powershell -ExecutionPolicy Bypass -File training\distributed\deploy_tiling.ps1

param(
    [string]$LaptopHost = "jared-laptop",
    [string]$ServerShare = "\\192.168.86.152\training",
    [string]$VideoShare = "\\192.168.86.152\video"
)

$cred = Get-Credential -Message "Enter laptop credentials" -UserName "training"

Invoke-Command -ComputerName $LaptopHost -Credential $cred -ScriptBlock {
    param($ServerShare, $VideoShare)

    $dst = "C:\soccer-cam-label"

    # Map training share
    net use T: $ServerShare /persistent:no 2>&1 | Out-Null
    net use V: $VideoShare /persistent:no 2>&1 | Out-Null

    # Copy tiling scripts from share
    $files = @("mass_tile.py", "extract_frames.py", "tile_frames.py", "game_registry.py")
    foreach ($f in $files) {
        Copy-Item "T:\_deploy\$f" "$dst\" -Force
        Write-Host "Copied $f"
    }

    # Also copy game registry JSON
    if (Test-Path "T:\game_registry.json") {
        Copy-Item "T:\game_registry.json" "$dst\" -Force
        Write-Host "Copied game_registry.json"
    }

    # Test imports
    $python = "C:\Python313\python.exe"
    $result = & $python -c "import sys; sys.path.insert(0, r'$dst'); from tile_frames import tile_frame; from extract_frames import extract_frames; print('OK')" 2>&1
    Write-Host "Import test: $result"

    if ($result -match "OK") {
        # Create a tiling runner script
        $script = @"
import sys, json, time, shutil, socket, glob as glob_mod, logging
from pathlib import Path

sys.path.insert(0, r'$dst')
from extract_frames import extract_frames
from tile_frames import tile_frame

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger()

HOSTNAME = socket.gethostname()
TILES_DIR = Path(r'T:\tiles_640')
VIDEO_SHARE = Path(r'V:\\')
REGISTRY = Path(r'T:\game_registry.json')
FRAME_INTERVAL = 4
DIFF_THRESHOLD = 2.0

with open(REGISTRY) as f:
    games = json.load(f)

for game in games:
    gid = game['game_id']
    tiles_dir = TILES_DIR / gid

    # Skip if already tiled
    if tiles_dir.exists():
        continue

    # Check lock
    lock_dir = TILES_DIR / '.locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f'{gid}.lock'
    if lock_file.exists() and (time.time() - lock_file.stat().st_mtime) < 7200:
        continue

    # Claim
    lock_file.write_text(f'{HOSTNAME} {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
    logger.info(f'Claimed {gid}')

    # Find video segments
    game_path = Path(game['path'])
    for i, part in enumerate(game_path.parts):
        if 'Flash' in part or 'Heat' in part:
            rel = Path(*game_path.parts[i:])
            source_dir = VIDEO_SHARE / rel
            break
    else:
        source_dir = VIDEO_SHARE / game_path.name

    segments = []
    for seg_name in game['segments']:
        matches = list(source_dir.rglob(seg_name))
        if matches:
            segments.append(matches[0])

    if not segments:
        logger.warning(f'No segments found for {gid}')
        lock_file.unlink(missing_ok=True)
        continue

    needs_flip = game.get('needs_flip', False)
    tiles_dir.mkdir(parents=True, exist_ok=True)

    for video in sorted(segments):
        seg_id = video.stem
        existing = list(tiles_dir.glob(f'{glob_mod.escape(seg_id)}_*_r0_c0.jpg'))
        if existing:
            continue

        frames_dir = Path(r'C:\soccer-cam-label\temp_frames') / seg_id
        n = extract_frames(video, frames_dir, diff_threshold=DIFF_THRESHOLD, frame_interval=FRAME_INTERVAL, flip=needs_flip)
        for fp in sorted(frames_dir.rglob('*.jpg')):
            tile_frame(fp, tiles_dir, cols=7, rows=3, tile_size=640)
        shutil.rmtree(frames_dir, ignore_errors=True)
        logger.info(f'  {seg_id}: {n} frames tiled')

    lock_file.unlink(missing_ok=True)
    logger.info(f'Done {gid}')
"@
        Set-Content -Path "$dst\tile_remote.py" -Value $script
        Write-Host "Created tile_remote.py"

        # Start tiling in background via WMI
        $cmdLine = "C:\Python313\python.exe -u $dst\tile_remote.py"
        $process = Start-Process -FilePath "C:\Python313\python.exe" -ArgumentList "-u","$dst\tile_remote.py" -NoNewWindow -PassThru -RedirectStandardOutput "$dst\tiling.log" -RedirectStandardError "$dst\tiling_err.log"
        Write-Host "Started tiling, PID: $($process.Id)"
    }
} -ArgumentList $ServerShare, $VideoShare
