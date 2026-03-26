import subprocess, os, sys, json, logging
from pathlib import Path

os.chdir(r'C:\soccer-cam-label')
sys.path.insert(0, r'C:\soccer-cam-label')

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s',
    handlers=[logging.FileHandler('labeling.log', mode='w'), logging.StreamHandler()])
logger = logging.getLogger()

# Map share
logger.info('Mapping share...')
subprocess.run(['net', 'use', 'Z:', '/delete', '/y'], capture_output=True)
r = subprocess.run(['net', 'use', 'Z:', r'\\192.168.86.152\video',
    '/user:DESKTOP-5L867J8\training', 'amy4ever', '/persistent:yes'],
    capture_output=True, text=True)
logger.info('net use: %s %s', r.stdout.strip(), r.stderr.strip())

games = {
    'heat__Clarence_Tournament': r'Z:\Heat_2012s\07.20.2024-07.21.2024 - Clarence Tournament',
    'heat__Heat_Tournament': r'Z:\Heat_2012s\06.07.2024-06.09.2024 - Heat Tournament',
}

from label_job import run_label_job

for game_id, src in games.items():
    dest = Path(f'videos/{game_id}')
    dest.mkdir(parents=True, exist_ok=True)
    
    # Copy if needed
    if not list(dest.rglob('*.mp4')):
        logger.info('Copying %s...', game_id)
        r = subprocess.run(['robocopy', src, str(dest), '*.mp4', '/S', '/MT:4'],
            capture_output=True, text=True)
        mp4s = list(dest.rglob('*.mp4'))
        logger.info('  Copied %d mp4 files', len(mp4s))
    
    # Label
    output_dir = Path(f'output/{game_id}')
    logger.info('=== Labeling %s ===', game_id)
    run_label_job(video_dir=dest, model_path=Path('models/balldet_fp16.onnx'),
        output_dir=output_dir, conf=0.45, frame_interval=4)
    
    # Delete videos to save space
    import shutil
    shutil.rmtree(str(dest), ignore_errors=True)
    logger.info('Cleaned up videos for %s', game_id)

# Copy ALL results back
logger.info('Copying all results back...')
subprocess.run(['net', 'use', 'Z:', r'\\192.168.86.152\video',
    '/user:DESKTOP-5L867J8\training', 'amy4ever'], capture_output=True)
for game_id in list(games.keys()) + ['heat__05.31.2024_vs_Fairport_home', 'heat__06.20.2024_vs_Chili_away', 'heat__07.17.2024_vs_Fairport_away']:
    src_dir = Path(f'output/{game_id}')
    if src_dir.exists() and list(src_dir.glob('*.txt')):
        dest_dir = f'Z:\training_data\labels_640_ext\{game_id}'
        subprocess.run(['robocopy', str(src_dir), dest_dir, '/MIR', '/MT:8'], capture_output=True)
        logger.info('Copied %s', game_id)

logger.info('=== ALL DONE ===')
