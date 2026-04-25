# Ball Tracking TODO List

## Currently Running
- [ ] **FP16 batch labeling** — Running on all 9 games with FP16 model (conf=0.25)
  - Background task ID: bysoyntpt
  - Output: F:/training_data/labels_640_ext/{game}/
  - Pipeline: detect at 4096x1800 → field filter → static removal → per-tile YOLO labels
  - ETA: ~5 days at current rate (0.4 f/s on GTX 1060)
  - **Consider**: running at 2048x900 instead of full res to speed up 4x

## Tracking Lab (current segment)
- [x] Segment: flash__09.30.2024_vs_Chili_home / 18.19.36-18.36.17
- [x] External detections generated (INT8, F:/training_data/ext_detections_chili_seg5_clean.json)
- [ ] **Regenerate with FP16 detections** once batch labeling processes this segment
- [x] 83+ user marks collected (frames 0-736)
- [ ] Continue marking through the full 17-minute segment
- [ ] Auto-regenerate guide path working (every 10 marks)

## Detection Model Issues Identified
- External model finds ball 47% of the time within 100px of user marks
- Ball at mid-field (r1 tiles) is 8-12px — too small for reliable detection
- Right side of field (c5-c6) has sun glare — detection degrades
- Far field (r0 tiles) — ball is very small, detector mostly misses
- FP16 model finds MORE detections than INT8 at equivalent thresholds
- Static detection filter (positions appearing >100 frames) removes false positives

## Tracking Approach
- [x] Simple Kalman tracker — FAILED (commits to wrong detection, can't recover)
- [x] User-guided detection selection — WORKS (interpolate between marks, pick nearest det)
- [ ] Need to extrapolate forward/backward from marks more aggressively
- [ ] Consider: multi-hypothesis tracker that maintains top-N candidates

## Infrastructure
- [x] Tracking Lab web UI with timeline, tile viewer, mark/drag, feedback
- [x] Auto-regenerate guide every 10 marks
- [x] External ball detector (external_ball_detector.py)
- [x] Curved field boundary filter (external_field_detector.py)
- [x] Dewarp tiles (dewarp_tiles.py) — minimal improvement, deprioritized
- [x] Frame differencing — only works when guided by tracker prediction
- [x] Annotation server with no-cache headers, tile serving

## Label Pipeline
- [x] Bootstrap YOLO labels (labels_640/) — noisy, 88% static balls
- [x] Heuristic filter (labels_640_filtered/) — size/aspect/edge
- [x] Trajectory validator (labels_640_clean/) — link & keep traj≥3
- [ ] External model labels (labels_640_ext/) — IN PROGRESS (FP16 batch)
- [ ] Once ext labels done: update NTFS junctions to point at labels_640_ext/
- [ ] Retrain model on clean labels

## Next Steps (Priority Order)
1. Wait for FP16 batch labeling to finish (or speed it up)
2. Regenerate tracking lab with FP16 detections for current segment
3. Continue user marking to build ground truth
4. Once enough marks: train our own model on user-verified + external labels
5. Build TrackNet temporal model (3-frame input) for small ball detection
6. Run person detection bootstrap for player clustering validation

## Key Files
- training/inference/external_ball_detector.py — external ONNX detector
- training/inference/external_field_detector.py — field boundary filter
- training/annotation/tracking_lab.py — tracking lab data generator
- training/annotation/simple_tracker.py — velocity-gated tracker
- training/data_prep/dewarp_tiles.py — fisheye dewarper
- training/data_prep/frame_diff_detector.py — frame differencing
- training/data_prep/trajectory_analyzer.py — experiment runner
- training/static/annotate.html — web UI
- training/annotation_server.py — FastAPI server

## Experiment Log
- See: training/annotation/experiment_log.md
- See: review_packets/tracking_lab/experiment_log.md (gitignored)
