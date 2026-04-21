"""Remote training script — maps share via WNet, then trains YOLO."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from map_share import map_share

# Map the share
share = r"\\192.168.86.152\video"
ok = map_share(share, r"DESKTOP-5L867J8\training", "amy4ever")
print(f"Share mapped: {ok}", flush=True)

# Verify access
test_path = os.path.join(share, "training_data", "ball_dataset_v2", "images", "val")
if not os.path.exists(test_path):
    print(f"ERROR: Cannot access {test_path}", flush=True)
    sys.exit(1)

print("Dataset accessible!", flush=True)

# Detect GPU and pick model
import torch

gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
print(f"GPU: {gpu_name}", flush=True)

if "4070" in gpu_name:
    model_name, run_name, batch = "yolo11s.pt", "ball_v2_3class", 16
elif "3060" in gpu_name:
    model_name, run_name, batch = "yolo11n.pt", "ball_v2_3class_3060", 16
else:
    model_name, run_name, batch = "yolo11n.pt", "ball_v2_3class_nano", 8

print(f"Training {model_name} batch={batch} as {run_name}", flush=True)

from ultralytics import YOLO

model = YOLO(model_name)
model.train(
    data=os.path.join(os.path.dirname(__file__), "dataset.yaml"),
    epochs=100,
    imgsz=640,
    batch=batch,
    device=0,
    project=os.path.join(os.path.dirname(__file__), "runs"),
    name=run_name,
    patience=30,
    workers=0,
    deterministic=True,
)
