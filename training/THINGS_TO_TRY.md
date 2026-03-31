# Things to Try — Ball Detection Model

## Human-in-the-Loop Track Recovery

When the tracker has a solid ball trajectory (10+ seconds of continuous tracking) and then loses the ball mid-play (no detection in predicted area, ball didn't cross sideline), queue the event for human annotation:

**Show the human:**
1. Last ~3 seconds of tracked ball (context + momentum)
2. The frame(s) where tracking was lost, with predicted search area highlighted
3. A few frames forward showing where the ball reappears (if it does)

**Human provides:**
- Click/tap ball location in the "lost" frames
- Or classify: "out of play" / "occluded by player" / "can't find it"

**Value:**
- Ground truth labels for the HARDEST frames (exactly where the model fails)
- Occlusion annotations become training data (ball-behind-player scenarios)
- Stitches tracks back together for continuous coverage
- Every answer improves both the current game AND future model training
- Much higher value per human-second than reviewing random tiles

**Priority queue:** Rank lost-ball events by: (1) track length before loss (longer = higher value), (2) whether ball reappears nearby (likely findable), (3) game phase (active play > warmup).

**Implementation notes:**
- Extend annotation app with "track assist" mode showing sequential frames
- Tracker queues events via API to annotation server
- Results feed back into both track reconstruction AND v3+ training labels
- These are the hardest negatives/positives — exactly what active learning would select

## Label Quality Improvements

### Single-ball constraint during active play
During a game there is exactly ONE ball on the field. If we detect multiple "game_ball" in the same panoramic frame during active play (not warmup/halftime), at least N-1 are wrong. Use game phase labels + this constraint to automatically flag multi-ball frames. For flagged frames, keep only the detection most consistent with the trajectory. Free quality improvement — no model needed, just logic on existing labels + game phases.

### v2 model re-labeling + disagreement review
After v2 training, run it on all tiles alongside original ONNX labels. Three cases:
- **Both agree** → high confidence, keep the label
- **ONNX says ball, v2 says no** → likely FP, send to Sonnet/human review
- **v2 says ball, ONNX missed it** → likely FN, new training data candidate

Disagreements are the most informative samples. Much higher value than reviewing random tiles. This is essentially active learning but driven by model consensus instead of confidence thresholds.

### Per-sample training loss analysis
Track which samples have persistently high loss during training. A correctly-labeled sample should eventually have low loss. If a sample still has high loss after 50+ epochs, the model is "fighting" the label — it's likely mislabeled. Export high-loss samples for review. Could add a validation pass that logs per-image loss after each epoch.

### Optical flow label propagation
For frames with high-confidence labels (in trajectory + Sonnet verified), use `cv2.calcOpticalFlowPyrLK` to track the ball point to adjacent frames that have no detection. This generates labels for frames the model missed, derived from frames we trust. Quality check: if optical flow position diverges from trajectory prediction by >50px, discard.

### Position-aware label weighting
Sonnet QA showed r1_c5 and r1_c6 have highest FP rates (sun glare). Instead of treating all labels equally, weight by expected quality based on tile position. Labels from clean positions get full weight in training; labels from noisy positions get reduced weight (0.5x) or require extra verification before inclusion.

### Heuristic pre-filtering
Filter bootstrap detections by ball physics before training:
- Aspect ratio ~0.7-1.4 (ball is round, reject elongated detections)
- Bounding box width 0.01-0.04 normalized (6-25px in 640x640)
- Reject detections on extreme tile edges (partial objects)
- Fast, free, apply immediately after bootstrap labeling

### Temporal propagation (crop-and-zoom)
Use temporal coherence to find missed balls:
- Group tiles by (game, tile position), sort by frame number
- If frame N has a ball at (x,y) but frame N+1 has no detection at same tile position, crop ~200x200 around previous position, upscale to 640x640, re-run inference
- Ball goes from ~10px to ~30px — much easier to detect
- Essentially poor man's tracking, propagates detections through time

### Cross-model consensus
Run a second model (different YOLO variant or architecture) on the same tiles, keep only detections where both models agree (IoU overlap). Higher precision at the cost of recall.

### Vision LLM review (Claude API)
Draw bounding boxes on tiles, send to Claude vision, ask "is this a soccer ball?"
- Very accurate but expensive at scale
- Could sample 5-10K detections to estimate precision
- Or only review ambiguous confidence ranges (0.1-0.3)
- Could batch tiles into grids to reduce API calls

### Active learning loop
After training v1, run it on unlabeled tiles. Send low-confidence detections (model is uncertain) to human review via the annotation server. These are the most informative examples for improving the model.

## Training Improvements

### Increase batch size after testing
Current batch=6 OOMs on GTX 1060 with yolo11m. Try batch=4 as a stable default. If model is switched to yolo11n, batch=16+ should work.

### Try yolo11n first
Nano model (2.6M params) trains much faster. Train a quick v1 with noisy labels, use it for active learning, then train yolo11m on curated labels for v2.

### Mosaic augmentation tuning
Current mosaic=1.0. Since most tiles are negative (no ball), mosaic creates training images with 4 empty tiles. Consider reducing mosaic or increasing the ratio of positive examples.

### Copy-paste augmentation
Current copy_paste=0.3. Could increase this to synthetically place balls from labeled tiles into empty tiles, increasing positive example count.

### Freeze backbone layers
Freeze early YOLO layers (feature extraction) and only train the detection head. Faster training, less overfitting on noisy labels.

### Multi-scale training
Train with imgsz=640 but add multi_scale=0.5 to randomly vary input size. Helps the model generalize to balls at different distances from the camera.

## Data Improvements

### Weight negative examples down
~80% of tiles have no ball. Consider reducing negative example ratio (e.g., include only 30% of negatives) to balance the dataset better.

### Separate near-field vs far-field models
r1 tiles (far field) have tiny balls, r2 tiles (near field) have larger balls. Could train separate models optimized for each distance, or use the tile position as an input feature.

### Add data from different weather/lighting
Current dataset may be biased toward certain conditions. Check if games span different times of day, weather, seasons for diversity.

### Synthetic ball placement
Crop real balls from labeled tiles, paste them onto empty tiles at random positions with augmentation (blur, scale, brightness). Dramatically increases positive training examples without manual labeling.

## Inference/Deployment

### Sliding window at inference time
At inference, run detection on overlapping 640x640 crops of the full panorama. Stitch results with NMS. This matches the training tile structure.

### Temporal smoothing at inference
Apply Kalman filter or simple exponential smoothing to ball position across frames. Reject spurious single-frame detections, interpolate through brief occlusions.

### Confidence calibration
After training, analyze the confidence-vs-accuracy curve on the validation set. Set an optimal threshold that balances false positives and missed detections for the autocam use case.

### TensorRT/ONNX export for speed
Export the final model to TensorRT or ONNX for faster inference on the GTX 1060. Can significantly reduce per-tile inference time.
