import subprocess, os, sys, json, glob, logging
from pathlib import Path

os.chdir(r'C:\soccer-cam-label')
sys.path.insert(0, r'C:\soccer-cam-label')

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s',
    handlers=[logging.FileHandler('labeling.log', mode='w'), logging.StreamHandler()])
logger = logging.getLogger()

# Step 1: Map network share using IP (hostname fails in scheduled task context)
SERVER_IP = '192.168.86.152'
logger.info('Mapping network share via IP %s...', SERVER_IP)
# Try unmapping first in case stale
subprocess.run(['net', 'use', 'Z:', '/delete', '/y'], capture_output=True)
r = subprocess.run(['net', 'use', 'Z:', f'\\\\{SERVER_IP}\\video',
    '/user:DESKTOP-5L867J8\\training', 'amy4ever', '/persistent:yes'],
    capture_output=True, text=True)
logger.info('net use: %s %s', r.stdout.strip(), r.stderr.strip())

if not Path('Z:/').exists():
    logger.error('Share mapping FAILED. Trying UNC path directly...')
    # Fall back to UNC with IP
    UNC = f'\\\\{SERVER_IP}\\video'
else:
    UNC = 'Z:'

# Step 2: Copy videos
games = {
    'heat__05.31.2024_vs_Fairport_home': f'{UNC}\\Heat_2012s\\05.31.2024 - vs Fairport (home)',
    'heat__06.20.2024_vs_Chili_away': f'{UNC}\\Heat_2012s\\06.20.2024 - vs Chili (away)',
    'heat__07.17.2024_vs_Fairport_away': f'{UNC}\\Heat_2012s\\07.17.2024 - vs Fairport (away)',
    'heat__Clarence_Tournament': f'{UNC}\\Heat_2012s\\07.20.2024-07.21.2024 - Clarence Tournament',
    'heat__Heat_Tournament': f'{UNC}\\Heat_2012s\\06.07.2024-06.09.2024 - Heat Tournament',
}

for game_id, src in games.items():
    dest = Path(f'videos/{game_id}')
    dest.mkdir(parents=True, exist_ok=True)
    existing = list(dest.rglob('*.mp4'))
    if existing:
        logger.info('SKIP copy %s: already has %d mp4 files', game_id, len(existing))
        continue
    logger.info('Copying %s from %s...', game_id, src)
    r = subprocess.run(['robocopy', src, str(dest), '*.mp4', '/S', '/MT:4'],
        capture_output=True, text=True)
    mp4s = list(dest.rglob('*.mp4'))
    logger.info('  Copied %d mp4 files', len(mp4s))

# Step 3: Run labeling
from label_job import run_label_job

for game_id in games:
    video_dir = Path(f'videos/{game_id}')
    output_dir = Path(f'output/{game_id}')
    if not list(video_dir.rglob('*.mp4')):
        logger.warning('SKIP labeling %s: no videos', game_id)
        continue
    logger.info('=== Labeling %s ===', game_id)
    run_label_job(
        video_dir=video_dir,
        model_path=Path('models/model.onnx'),
        output_dir=output_dir,
        conf=0.45,
        frame_interval=4,
    )

# Step 4: Copy results back
output_share = f'{UNC}\\training_data\\labels_640_ext'
logger.info('Copying results to %s...', output_share)
for game_id in games:
    src_dir = Path(f'output/{game_id}')
    if src_dir.exists() and list(src_dir.glob('*.txt')):
        dest_dir = f'{output_share}\\{game_id}'
        r = subprocess.run(['robocopy', str(src_dir), dest_dir, '/MIR', '/MT:8'],
            capture_output=True, text=True)
        logger.info('  %s: copied', game_id)

logger.info('=== ALL DONE ===')
