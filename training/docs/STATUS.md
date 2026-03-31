# Current Status

*Last updated: 2026-03-31 12:00*

## Running Processes

| Process | Machine | Status | ETA |
|---------|---------|--------|-----|
| v2 training (YOLO11n, 3-class) | Laptop RTX 4070 | Epoch ~47/100, 2.4 it/s | ~18 hrs |
| Mass tiling (25 games) | Server CPU | Game 1/25, copying segments | ~13 hrs |
| OnceAutocam archive (D:→F:) | Server | ~4/77 files | ~15 hrs |

## Laptop Setup (jared-laptop)

Training user: `training`. Python: `C:\Python313\python.exe`. Scripts: `C:\soccer-cam-label\`.
No git, no uv — standalone scripts deployed via PS remoting + WNet share mapping.

**How to deploy new scripts:**
1. Copy files to `D:\training_data\_deploy\` on server (exposed as `\\192.168.86.152\training\_deploy\`)
2. PS remote to laptop, map share with `map_share.py` (WNet API), copy files to `C:\soccer-cam-label\`
3. Run via WMI `Start-Process` for persistent processes

**How to start remote tiling:**
```powershell
# From server PS remoting session:
$cred = Get-Credential training
Invoke-Command -ComputerName jared-laptop -Credential $cred -ScriptBlock {
    # Files should already be in C:\soccer-cam-label\ from deployment
    Start-Process -FilePath "C:\Python313\python.exe" -ArgumentList "-u","C:\soccer-cam-label\tile_remote.py" -NoNewWindow
}
```

**Training process:** PID 7804 running `C:\Python313\python.exe -u C:\soccer-cam-label\train_now.py`

## Next Steps

1. Run full mass tiling (26 untiled games) with `mass_tile.py` at frame_interval=4
2. After tiling: bootstrap labels on new games with ONNX detector
3. Human review of 328 ball verification frames
4. Build v3 dataset with all 35 games

## Dataset State

- **Games:** 35 total in registry, 9 tiled/labeled, 26 need tiling
- **v2 dataset:** 275K labeled pairs, 3-class (game_ball/static_ball/not_ball)
- **v2 training:** Running on laptop, server GPU available for data work

## Known Issues

- 2 games need code flip (no corrected video): `flash__2025.05.17_164952`, `heat__2024.06.27_vs_Pittsford_away`
- Ball verify UI has 328 unreviewed frames from RNYFC game
- Game phase detection shows unrealistic halftime durations for tournament games (sub-game breaks detected as single halftime)

## Recent Milestones

- [2026-03-31] Mass tiling pipeline ready (`mass_tile.py`): 35 games in registry, 9 tiled, 26 to go
- [2026-03-30] Game phase detection working for 9 games
- [2026-03-30] Ball verification UI live at https://trainer.goat-rattlesnake.ts.net:8642
- [2026-03-29] v2 dataset built: 275K labeled pairs, 3-class
- [2026-03-29] v2 training started on laptop RTX 4070
