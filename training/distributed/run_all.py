import sys, os, json, glob, logging

os.chdir(r'C:\soccer-cam-label')
sys.path.insert(0, r'C:\soccer-cam-label')

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(r'C:\soccer-cam-label\labeling.log', mode='w'),
        logging.StreamHandler()
    ]
)

from label_job import run_label_job
from pathlib import Path

configs = sorted(glob.glob(r'C:\soccer-cam-label\job_*.json'))
for cfg_path in configs:
    with open(cfg_path) as f:
        cfg = json.load(f)
    video_dir = Path(cfg['video_dir'])
    if not video_dir.exists():
        logging.warning(f'SKIP {cfg_path}: video dir not found: {video_dir}')
        continue
    logging.info(f'=== Starting {cfg_path} ===')
    run_label_job(
        video_dir=video_dir,
        model_path=Path(cfg['model']),
        output_dir=Path(cfg['output']),
        conf=cfg.get('conf', 0.45),
        frame_interval=cfg.get('frame_interval', 4),
    )

logging.info('=== ALL JOBS COMPLETE ===')
