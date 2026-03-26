import sys, os, logging, json
from pathlib import Path

os.chdir(r'C:\soccer-cam-label')
sys.path.insert(0, r'C:\soccer-cam-label')

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s',
    handlers=[logging.FileHandler('labeling.log', mode='w'), logging.StreamHandler()])

from label_job import run_label_job

games = ['heat__05.31.2024_vs_Fairport_home', 'heat__06.20.2024_vs_Chili_away', 'heat__07.17.2024_vs_Fairport_away']

for game_id in games:
    video_dir = Path(f'videos/{game_id}')
    output_dir = Path(f'output/{game_id}')
    mp4s = list(video_dir.rglob('*.mp4'))
    if not mp4s:
        logging.warning('SKIP %s: no mp4s', game_id)
        continue
    logging.info('=== %s: %d segments ===', game_id, len(mp4s))
    run_label_job(video_dir=video_dir, model_path=Path('models/model.onnx'),
        output_dir=output_dir, conf=0.45, frame_interval=4)

# Copy results back
import subprocess
logging.info('Copying results back...')
subprocess.run(['net', 'use', 'Z:', r'\192.168.86.152\video',
    '/user:DESKTOP-5L867J8\training', 'amy4ever', '/persistent:yes'],
    capture_output=True)
for game_id in games:
    src = Path(f'output/{game_id}')
    if src.exists() and list(src.glob('*.txt')):
        dest = f'Z:\training_data\labels_640_ext\{game_id}'
        subprocess.run(['robocopy', str(src), dest, '/MIR', '/MT:8'], capture_output=True)
        logging.info('Copied %s', game_id)

logging.info('=== ALL DONE ===')
