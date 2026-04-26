# Virtual Broadcast Camera — Ball Position Only

Render a broadcast-style cropped video from a wide-angle static camera using only ball position as input. The system digitally pans and zooms to simulate a professional camera operator.

## Rendering Layer

Output frames are produced by **cylindrical projection**, not flat 2D crop. The source is a stitched ~180° panorama that is still effectively fisheye-curved (`StitchCorrectStage` reduces inter-camera stitch artifacts but does not de-fisheye); a flat crop of that source curves goal lines and stretches players near output-frame edges. A cylindrical render projects the source onto a virtual cylinder and renders a perspective view from inside it, keeping straight lines straight at all yaw angles.

Implementation lives in `video_grouper/inference/cylindrical_view.py` and is invoked from the render stage (`video_grouper/ball_tracking/providers/homegrown/stages/render.py`). The control logic described below — pan target, lead room, zoom selection, dead-ball overrides, broadcast vs coach modes — operates on yaw/pitch/zoom inputs to that renderer rather than on a 2D crop rectangle, but the algorithm is unchanged. Where the spec below talks about "crop rectangle" or "crop width", read it as "rendered viewport" or "viewport horizontal field of view".

## Inputs

- **Ball position** (x, y) per frame in pixel coordinates
- **Source video** resolution (e.g., 7680x2160 from Reolink, 4096x1800 from Dahua)
- **Source horizontal FOV** (default 180°)
- **Output resolution** (e.g., 1920x1080)

## Core Concepts

### Pan: Follow the Ball with Lead Room

The crop region follows the ball horizontally, but the ball is NOT centered in the frame. It is offset so there is extra space ("lead room") in the direction the ball is traveling. This gives the viewer a sense of momentum and lets them see what's coming.

```
Ball moving right →

    ┌─────────────────────────────┐
    │         ●→                  │
    │     ball    lead room →     │
    └─────────────────────────────┘

Ball moving left ←

    ┌─────────────────────────────┐
    │                  ←●         │
    │     ← lead room    ball    │
    └─────────────────────────────┘
```

**Lead room amount** scales with ball speed:
- Slow/stationary ball: minimal offset, nearly centered
- Fast ball: larger offset in the direction of travel (up to ~20% of crop width)

### Zoom: Field Zone + Ball Speed

Without player position data, zoom level is driven by two proxies:

1. **Where on the field the ball is** (field zone)
2. **How fast the ball is moving** (ball speed)

These approximate what a camera operator does intuitively: zoom in when action is concentrated (near goal), zoom out when the ball is traveling long distance.

## Algorithm

### Per-Frame Pipeline

```
For each frame:
  1. Read ball position (x, y)
  2. Compute ball velocity (dx, dy) from previous frame(s)
  3. Compute ball speed = sqrt(dx^2 + dy^2)
  4. Determine field zone from ball x-position
  5. Compute target zoom level from zone + speed
  6. Compute pan target (ball position + lead room offset)
  7. Apply dead-ball overrides if ball is stationary
  8. Smooth pan and zoom targets (prevent jitter)
  9. Compute crop rectangle from smoothed pan + zoom
  10. Clamp crop rectangle to source frame bounds
  11. Extract and scale crop to output resolution
```

### Step 1-3: Ball Kinematics

Compute velocity as an exponential moving average over recent frames to filter noise:

```
alpha = 0.3
velocity_x = alpha * (x - prev_x) + (1 - alpha) * prev_velocity_x
velocity_y = alpha * (y - prev_y) + (1 - alpha) * prev_velocity_y
speed = sqrt(velocity_x^2 + velocity_y^2)
```

### Step 4: Field Zone Classification

Map the ball's x-position to a zone. The field runs left-to-right in the camera view. Normalize ball x to [0, 1] across the frame width.

| Ball x (normalized) | Zone | Description |
|---|---|---|
| 0.00 - 0.10 | left_box | Left penalty area |
| 0.10 - 0.33 | left_third | Left defensive/attacking third |
| 0.33 - 0.67 | midfield | Middle third |
| 0.67 - 0.90 | right_third | Right defensive/attacking third |
| 0.90 - 1.00 | right_box | Right penalty area |

### Step 5: Target Zoom Level

Zoom is expressed as **crop width** relative to the source frame. Smaller crop = more zoomed in.

| Zone | Base crop width (fraction of source) |
|---|---|
| left_box / right_box | 0.25 (tight) |
| left_third / right_third | 0.35 (medium) |
| midfield | 0.45 (wide) |

**Speed modifier**: fast ball movement biases the zoom wider (need to show where the ball is going).

```
speed_normalized = clamp(speed / max_expected_speed, 0, 1)
speed_zoom_bias = speed_normalized * 0.10   # up to 10% wider at max speed
target_crop_width = base_crop_width + speed_zoom_bias
```

These values are starting points — tune based on actual source resolution and field of view.

### Step 6: Lead Room Offset

Offset the pan target from the ball position in the direction of travel:

```
max_lead_room = 0.20 * crop_width   # up to 20% of the crop width
lead_factor = clamp(speed / max_expected_speed, 0, 1)

lead_offset_x = -velocity_x_normalized * lead_factor * max_lead_room
lead_offset_y = -velocity_y_normalized * lead_factor * max_lead_room * 0.5  # less vertical lead

pan_target_x = ball_x + lead_offset_x
pan_target_y = ball_y + lead_offset_y
```

The offset is in the direction the ball is heading, so the ball sits behind center and the space ahead is visible.

### Step 7: Dead-Ball Overrides

When the ball is stationary (speed < threshold for N consecutive frames), classify by position and override framing:

| Ball stationary at | Override behavior |
|---|---|
| Corner arc region | Pan to the near goal mouth, zoom tight |
| Near sideline | Hold position, medium zoom |
| Center circle | Wide zoom, centered |
| In a penalty box | Tight zoom on the box, centered on goal |
| Elsewhere | Hold last framing, slight zoom out |

**Corner kick special case**: When the ball is stationary near a corner flag, the interesting action is at the goal, not at the ball. Shift the pan target toward the near goal (roughly 10-15% of frame width inward from the corner, vertically toward the goal center).

### Step 8: Smoothing

All pan and zoom transitions must be smoothed to avoid jarring cuts. Use exponential smoothing with different rates for pan vs zoom:

```
pan_smoothing = 0.08    # faster response — needs to track the ball
zoom_smoothing = 0.03   # slower response — zoom changes should feel gradual

smoothed_pan_x = smoothed_pan_x + pan_smoothing * (target_pan_x - smoothed_pan_x)
smoothed_pan_y = smoothed_pan_y + pan_smoothing * (target_pan_y - smoothed_pan_y)
smoothed_zoom  = smoothed_zoom  + zoom_smoothing * (target_zoom - smoothed_zoom)
```

Pan smoothing should be **asymmetric**: faster to catch up when the ball moves quickly, slower to settle when the ball slows down. Scale `pan_smoothing` with ball speed:

```
pan_smoothing = lerp(0.04, 0.12, clamp(speed / max_expected_speed, 0, 1))
```

### Step 9-10: Crop Rectangle

```
crop_w = smoothed_zoom * source_width
crop_h = crop_w * (output_height / output_width)   # maintain output aspect ratio

crop_x = smoothed_pan_x - crop_w / 2
crop_y = smoothed_pan_y - crop_h / 2

# Clamp to source bounds
crop_x = clamp(crop_x, 0, source_width - crop_w)
crop_y = clamp(crop_y, 0, source_height - crop_h)
```

### Step 11: Render

Extract the crop rectangle from the source frame and resize to output resolution. Use a high-quality interpolation method (Lanczos or bicubic) for the downscale.

## Missing Ball Handling

When ball detection fails (occluded, out of frame, detection miss):

- **Short gap (< 0.5s)**: Interpolate ball position linearly between last known and next known position. Hold current pan/zoom.
- **Medium gap (0.5s - 2s)**: Slowly zoom out to a wider view while holding last pan direction. This is the safe default — show more context when uncertain.
- **Long gap (> 2s)**: Zoom to a wide default framing (midfield, ~50% crop width). Resume tracking when ball reappears.

Do not snap the camera when the ball reappears — let the smoothing algorithm ease back to tracking.

## Tunable Parameters

| Parameter | Default | Description |
|---|---|---|
| `pan_smoothing_min` | 0.04 | Pan EMA alpha when ball is slow |
| `pan_smoothing_max` | 0.12 | Pan EMA alpha when ball is fast |
| `zoom_smoothing` | 0.03 | Zoom EMA alpha |
| `max_lead_room_fraction` | 0.20 | Max lead room as fraction of crop width |
| `max_expected_speed` | TBD | Ball speed (px/frame) that maps to "max" — calibrate per camera |
| `dead_ball_speed_threshold` | TBD | Speed below which ball is considered stationary |
| `dead_ball_frame_count` | 15 | Frames ball must be stationary before dead-ball override |
| `zone_box_boundary` | 0.10 | Normalized x marking the penalty box edge |
| `zone_third_boundary` | 0.33 | Normalized x marking the third boundaries |
| `zoom_box` | 0.25 | Crop width fraction when ball is in the box |
| `zoom_third` | 0.35 | Crop width fraction in attacking/defending third |
| `zoom_midfield` | 0.45 | Crop width fraction at midfield |
| `zoom_speed_bias_max` | 0.10 | Max additional crop width from speed |
| `missing_ball_short_threshold` | 15 | Frames before "short gap" handling ends |
| `missing_ball_medium_threshold` | 60 | Frames before switching to wide default |

## Vertical Framing

The vertical (y-axis) crop position is less dynamic than horizontal:

- The field is roughly centered vertically in the wide camera view
- Vertical pan target follows the ball y-position but with heavier smoothing (field doesn't extend as far vertically)
- When zoomed out wide, vertical position should favor centering on the field rather than the ball
- When zoomed in tight, vertical position tracks the ball more closely

## Camera Modes

The algorithm supports two modes that share the same pipeline but use different parameter values. Mode is selected at render time.

### Broadcast Mode (default)

Optimized for watching the game. Zooms in on the action, creates drama, follows the ball tightly with lead room for anticipation. This is what TV broadcasts look like.

### Coach Mode

Optimized for tactical analysis. Prioritizes team shape, spacing, and off-ball movement over close-up action. Key differences from broadcast mode:

**Wider zoom floor**: The camera never zooms in as tight. Coaches need to see defensive lines, pressing shape, and weak-side positioning. Even when the ball is in the box, the framing stays wide enough to show the full defensive setup.

**Reduced lead room**: Lead room is cut in half or eliminated. Coaches want to see what's behind the ball — are players recovering? Is the midfield compact? Centering the ball gives equal visibility in both directions.

**Inverted dead-ball overrides**: Where broadcast mode zooms *in* on set pieces for drama, coach mode zooms *out* to show the full set piece organization: marking assignments, runner positions, players left back.

**Slower, steadier zoom**: Zoom transitions are even more gradual. On a counterattack, broadcast mode tightens on the ball carrier for excitement. Coach mode stays wide to show the defensive shape collapsing (or not) and whether attackers are making runs.

**Less vertical tracking**: Vertical pan tracks the field center more than the ball, keeping the full width of the field visible even when the ball is near a touchline.

### Parameter Overrides by Mode

| Parameter | Broadcast | Coach |
|---|---|---|
| `zoom_box` | 0.25 | 0.40 |
| `zoom_third` | 0.35 | 0.50 |
| `zoom_midfield` | 0.45 | 0.55 |
| `zoom_speed_bias_max` | 0.10 | 0.05 |
| `max_lead_room_fraction` | 0.20 | 0.08 |
| `pan_smoothing_min` | 0.04 | 0.03 |
| `pan_smoothing_max` | 0.12 | 0.08 |
| `zoom_smoothing` | 0.03 | 0.02 |

Dead-ball overrides in coach mode:

| Ball stationary at | Broadcast override | Coach override |
|---|---|---|
| Corner arc region | Pan to goal, zoom tight | Pan to show corner + goal, zoom wide |
| Near sideline | Hold, medium zoom | Hold, zoom out slightly |
| Center circle | Wide, centered | Wide, centered (same) |
| In a penalty box | Tight on box | Medium-wide to show full box + top |
| Elsewhere | Hold, slight zoom out | Hold, slight zoom out (same) |

## Output

- Resolution: 1920x1080 (or configurable)
- Codec: H.264 (mp4)
- Frame rate: match source
- The output should look like a single continuous camera shot with smooth, professional panning and zooming
