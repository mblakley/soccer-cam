"""Game phase detection — identify pre-game, halves, halftime, post-game.

Three detection signals, layered by reliability:
  1. Whistle detection (audio) — precise boundaries from referee whistles
  2. Sonnet vision (sampled tiles) — classifies scenes by visual cues
  3. Multi-ball heuristic — fallback using label statistics

Runs as "Phase 0" at the start of sonnet_qa before label QA begins.
Writes results to the game_phases table in the per-game manifest.

Pull-local-process-push pattern:
  - Needs video files for whistle detection (pulled to local SSD)
  - Needs pack files for Sonnet vision (center tiles sampled)
  - Writes to manifest.db (already pulled by sonnet_qa caller)
"""

import json
import logging
import subprocess
import time
from pathlib import Path

import numpy as np

from training.data_prep.game_manifest import GameManifest
from training.tasks.io import TaskIO

logger = logging.getLogger(__name__)

# Audio analysis parameters
WHISTLE_FREQ_LOW = 2000  # Hz — referee whistles are typically 2-4 kHz
WHISTLE_FREQ_HIGH = 4500  # Hz
WHISTLE_MIN_DURATION = 0.3  # seconds — minimum whistle blast
WHISTLE_ENERGY_PERCENTILE = 97  # energy threshold for whistle band
WHISTLE_CLUSTER_GAP = 10.0  # seconds — merge whistles within this gap

# Sonnet sampling parameters
SONNET_SAMPLE_INTERVAL_SEC = 60  # sample 1 frame per minute
SONNET_GRID_COLS = 4
SONNET_GRID_ROWS = 2
SONNET_TILES_PER_GRID = SONNET_GRID_COLS * SONNET_GRID_ROWS

# Video parameters (matching tile task)
FPS = 30
FRAME_INTERVAL = 4  # tiles extracted every 4 frames


def detect_game_phases(
    manifest: GameManifest,
    task_io: TaskIO,
    *,
    force: bool = False,
) -> dict:
    """Run game phase detection and store results in manifest.

    Returns summary dict with detected phases.
    """
    # Check if phases already exist
    existing = manifest.get_metadata("game_phases_summary")
    if existing and not force:
        logger.info("Game phases already detected for %s, skipping", manifest.game_id)
        return json.loads(existing)

    logger.info("Starting game phase detection for %s", manifest.game_id)

    segments = manifest.get_segments()
    if not segments:
        logger.warning("No segments found for %s", manifest.game_id)
        return {}

    segments.sort()  # chronological order (segment names encode time)

    # Build segment timeline
    seg_timeline = _build_segment_timeline(segments, manifest)
    if not seg_timeline:
        logger.warning("Could not build timeline for %s", manifest.game_id)
        return {}

    # Signal 1: Whistle detection from audio
    whistle_phases = _detect_whistles(task_io, seg_timeline)

    # Signal 2: Sonnet vision classification
    sonnet_phases = _detect_with_sonnet(manifest, task_io, seg_timeline)

    # Merge signals: whistle takes priority, Sonnet fills gaps
    phases = _merge_phase_signals(whistle_phases, sonnet_phases, seg_timeline)

    if phases:
        # Determine the primary source
        source = "whistle" if whistle_phases else "sonnet"
        manifest.replace_phases(phases, source)
        logger.info(
            "Detected %d phases for %s (source=%s): %s",
            len(phases),
            manifest.game_id,
            source,
            ", ".join(p["phase"] for p in phases),
        )
    else:
        logger.warning("No phases detected for %s", manifest.game_id)

    return {
        "phase_count": len(phases),
        "phases": [p["phase"] for p in phases],
        "source": "whistle" if whistle_phases else "sonnet",
    }


# ------------------------------------------------------------------
# Segment timeline
# ------------------------------------------------------------------


def parse_segment_time(segment_name: str) -> float | None:
    """Extract start time in seconds from segment name.

    Handles Dahua format: '18.01.30-18.18.20[F][0@0]...'
    """
    try:
        parts = segment_name.split("-")
        start = parts[0]
        h = int(start[0:2])
        m = int(start[3:5])
        s = int(start[6:8])
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return None


def _build_segment_timeline(segments: list[str], manifest: GameManifest) -> list[dict]:
    """Build a timeline mapping segments to absolute times and frame ranges."""
    timeline = []
    for seg in segments:
        start_sec = parse_segment_time(seg)
        if start_sec is None:
            continue

        seg_info = manifest.conn.execute(
            "SELECT frame_min, frame_max, frame_count FROM segments WHERE segment = ?",
            (seg,),
        ).fetchone()
        if not seg_info:
            continue

        frame_min = seg_info[0] or 0
        frame_max = seg_info[1] or 0
        duration_sec = (frame_max - frame_min) / FPS

        timeline.append(
            {
                "segment": seg,
                "start_sec": start_sec,
                "end_sec": start_sec + duration_sec,
                "frame_min": frame_min,
                "frame_max": frame_max,
            }
        )

    timeline.sort(key=lambda t: t["start_sec"])
    return timeline


def frame_to_abs_time(segment: str, frame_idx: int, timeline: list[dict]) -> float:
    """Convert a (segment, frame_idx) to absolute seconds."""
    for t in timeline:
        if t["segment"] == segment:
            offset = (frame_idx - t["frame_min"]) / FPS
            return t["start_sec"] + offset
    return 0.0


def abs_time_to_frame(abs_sec: float, timeline: list[dict]) -> tuple[str, int] | None:
    """Convert absolute seconds to (segment, frame_idx)."""
    for t in timeline:
        if t["start_sec"] <= abs_sec <= t["end_sec"]:
            offset_sec = abs_sec - t["start_sec"]
            frame_idx = t["frame_min"] + int(offset_sec * FPS)
            # Snap to frame interval
            frame_idx = (frame_idx // FRAME_INTERVAL) * FRAME_INTERVAL
            frame_idx = min(frame_idx, t["frame_max"])
            return t["segment"], frame_idx
    return None


# ------------------------------------------------------------------
# Signal 1: Whistle detection (audio)
# ------------------------------------------------------------------


def _detect_whistles(task_io: TaskIO, timeline: list[dict]) -> list[dict] | None:
    """Detect referee whistles from video audio tracks.

    Returns list of phase dicts if successful, None if audio unavailable.
    """
    try:
        import importlib.util

        if importlib.util.find_spec("av") is None:
            raise ImportError("av not installed")
    except ImportError:
        logger.info("PyAV not available, skipping whistle detection")
        return None

    # Find video files
    video_dir = task_io.local_video
    if not video_dir.exists():
        try:
            task_io.pull_video()
        except (FileNotFoundError, OSError) as e:
            logger.info("Video not available for whistle detection: %s", e)
            return None

    video_files = sorted(video_dir.glob("*.mp4"))
    if not video_files:
        logger.info("No video files found for whistle detection")
        return None

    # Extract whistle timestamps from each segment
    all_whistles = []  # list of absolute-time whistle events

    for vf in video_files:
        seg_name = vf.stem
        seg_info = next((t for t in timeline if t["segment"] == seg_name), None)
        if not seg_info:
            # Try partial match (segment name may not exactly match filename)
            for t in timeline:
                if t["segment"].startswith(seg_name[:8]):
                    seg_info = t
                    break
        if not seg_info:
            continue

        try:
            whistles = _extract_whistles_from_video(vf)
            for w_sec in whistles:
                abs_time = seg_info["start_sec"] + w_sec
                all_whistles.append(abs_time)
        except Exception as e:
            logger.warning("Whistle extraction failed for %s: %s", vf.name, e)
            continue

    if not all_whistles:
        logger.info("No whistles detected in audio")
        return None

    all_whistles.sort()
    logger.info(
        "Detected %d whistle events across %d segments",
        len(all_whistles),
        len(video_files),
    )

    # Cluster whistles (referee often blows multiple short blasts)
    clusters = _cluster_whistle_events(all_whistles)
    logger.info("Whistle clusters: %s", [f"{c:.0f}s" for c in clusters])

    # Classify clusters into game events
    return _classify_whistle_clusters(clusters, timeline)


def _extract_whistles_from_video(video_path: Path) -> list[float]:
    """Extract whistle timestamps from a video file's audio track.

    Returns list of timestamps (seconds from start of video) where
    referee whistles are detected.
    """
    import av

    container = av.open(str(video_path))

    # Check for audio stream
    if not container.streams.audio:
        container.close()
        return []

    audio_stream = container.streams.audio[0]
    sample_rate = audio_stream.rate or 44100

    # Decode audio to numpy array
    samples = []
    try:
        for frame in container.decode(audio=0):
            arr = frame.to_ndarray()
            # Convert to mono if stereo
            if arr.ndim > 1:
                arr = arr.mean(axis=0)
            samples.append(arr)
    except Exception as e:
        logger.debug("Audio decode error in %s: %s", video_path.name, e)
    finally:
        container.close()

    if not samples:
        return []

    audio = np.concatenate(samples).astype(np.float32)

    # Analyze in windows for whistle frequency energy
    return _find_whistle_timestamps(audio, sample_rate)


def _find_whistle_timestamps(audio: np.ndarray, sample_rate: int) -> list[float]:
    """Find timestamps where whistle-frequency energy exceeds threshold.

    Uses short-time FFT to detect sustained high-energy in the 2-4.5 kHz band.
    """
    window_size = int(sample_rate * 0.1)  # 100ms windows
    hop_size = window_size // 2  # 50ms hop

    if len(audio) < window_size:
        return []

    # Frequency bin indices for whistle band
    freqs = np.fft.rfftfreq(window_size, d=1.0 / sample_rate)
    whistle_mask = (freqs >= WHISTLE_FREQ_LOW) & (freqs <= WHISTLE_FREQ_HIGH)
    total_mask = freqs > 500  # ignore below 500Hz (crowd noise, wind)

    # Compute energy in whistle band for each window
    n_windows = (len(audio) - window_size) // hop_size + 1
    whistle_energy = np.zeros(n_windows)
    total_energy = np.zeros(n_windows)

    hann = np.hanning(window_size)

    for i in range(n_windows):
        start = i * hop_size
        chunk = audio[start : start + window_size] * hann
        spectrum = np.abs(np.fft.rfft(chunk))

        whistle_energy[i] = np.sum(spectrum[whistle_mask] ** 2)
        total_energy[i] = np.sum(spectrum[total_mask] ** 2) + 1e-10

    # Whistle ratio: what fraction of energy is in the whistle band
    whistle_ratio = whistle_energy / total_energy

    # Threshold: whistles have high absolute energy AND high ratio
    energy_threshold = np.percentile(whistle_energy, WHISTLE_ENERGY_PERCENTILE)
    ratio_threshold = 0.15  # whistle band should be >15% of total energy

    is_whistle = (whistle_energy > energy_threshold) & (whistle_ratio > ratio_threshold)

    # Find contiguous whistle regions
    timestamps = []
    in_whistle = False
    whistle_start = 0

    for i in range(len(is_whistle)):
        t = i * hop_size / sample_rate
        if is_whistle[i] and not in_whistle:
            in_whistle = True
            whistle_start = t
        elif not is_whistle[i] and in_whistle:
            in_whistle = False
            duration = t - whistle_start
            if duration >= WHISTLE_MIN_DURATION:
                timestamps.append(whistle_start + duration / 2)  # midpoint

    # Handle whistle at end of audio
    if in_whistle:
        t_end = len(audio) / sample_rate
        duration = t_end - whistle_start
        if duration >= WHISTLE_MIN_DURATION:
            timestamps.append(whistle_start + duration / 2)

    return timestamps


def _cluster_whistle_events(timestamps: list[float]) -> list[float]:
    """Cluster nearby whistle events into single game events.

    A referee may blow 3-5 short blasts — these cluster into one event.
    Returns the center timestamp of each cluster.
    """
    if not timestamps:
        return []

    clusters = []
    cluster_start = timestamps[0]
    cluster_end = timestamps[0]

    for t in timestamps[1:]:
        if t - cluster_end <= WHISTLE_CLUSTER_GAP:
            cluster_end = t
        else:
            clusters.append((cluster_start + cluster_end) / 2)
            cluster_start = t
            cluster_end = t

    clusters.append((cluster_start + cluster_end) / 2)
    return clusters


def _classify_whistle_clusters(
    clusters: list[float], timeline: list[dict]
) -> list[dict] | None:
    """Classify whistle clusters into game phase boundaries.

    Expected pattern for a standard game:
    - Whistle 1: game start (kickoff)
    - Whistle 2: halftime start (~35-45 min later)
    - Whistle 3: second half start (~10-20 min after halftime)
    - Whistle 4: game end (~35-45 min after second half start)

    Returns phase list or None if pattern doesn't match.
    """
    if len(clusters) < 2:
        return None

    game_start_sec = timeline[0]["start_sec"]
    game_end_sec = timeline[-1]["end_sec"]

    # Find the best 4-whistle pattern (or 2-whistle for short games)
    # Score by how well gaps match expected half lengths
    best_phases = None
    best_score = float("inf")

    EXPECTED_HALF = 40 * 60  # 40 minutes
    EXPECTED_BREAK = 10 * 60  # 10 minutes

    # Try all combinations of 4 clusters
    if len(clusters) >= 4:
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                for k in range(j + 1, len(clusters)):
                    for m in range(k + 1, len(clusters)):
                        c = [clusters[i], clusters[j], clusters[k], clusters[m]]
                        h1 = c[1] - c[0]  # first half duration
                        brk = c[2] - c[1]  # halftime duration
                        h2 = c[3] - c[2]  # second half duration

                        # Score: deviation from expected pattern
                        score = (
                            abs(h1 - EXPECTED_HALF)
                            + abs(brk - EXPECTED_BREAK) * 0.5
                            + abs(h2 - EXPECTED_HALF)
                        )

                        # Reject if halves are too short or too long
                        if h1 < 15 * 60 or h1 > 55 * 60:
                            continue
                        if h2 < 15 * 60 or h2 > 55 * 60:
                            continue
                        if brk < 2 * 60 or brk > 30 * 60:
                            continue

                        if score < best_score:
                            best_score = score
                            best_phases = _build_phases_from_whistles(
                                c, game_start_sec, game_end_sec, timeline
                            )

    # Fallback: try 2-whistle pattern (just game start + end)
    if best_phases is None and len(clusters) >= 2:
        # Pick first and last cluster
        c0, c1 = clusters[0], clusters[-1]
        game_duration = c1 - c0
        if 50 * 60 <= game_duration <= 120 * 60:
            # Reasonable game length — assume no detected halftime
            best_phases = _build_phases_from_whistles(
                [c0, c1], game_start_sec, game_end_sec, timeline
            )

    return best_phases


def _build_phases_from_whistles(
    whistle_times: list[float],
    game_start_sec: float,
    game_end_sec: float,
    timeline: list[dict],
) -> list[dict]:
    """Build phase list from classified whistle timestamps."""
    phases = []

    first_seg = timeline[0]["segment"]
    first_frame = timeline[0]["frame_min"]
    last_seg = timeline[-1]["segment"]
    last_frame = timeline[-1]["frame_max"]

    if len(whistle_times) >= 4:
        # Full pattern: pre-game, first half, halftime, second half, post-game
        boundaries = [
            ("pre_game", game_start_sec, whistle_times[0]),
            ("first_half", whistle_times[0], whistle_times[1]),
            ("halftime", whistle_times[1], whistle_times[2]),
            ("second_half", whistle_times[2], whistle_times[3]),
            ("post_game", whistle_times[3], game_end_sec),
        ]
    elif len(whistle_times) == 2:
        boundaries = [
            ("pre_game", game_start_sec, whistle_times[0]),
            ("first_half", whistle_times[0], whistle_times[1]),
            ("post_game", whistle_times[1], game_end_sec),
        ]
    else:
        return []

    for phase_name, start_abs, end_abs in boundaries:
        start_loc = abs_time_to_frame(start_abs, timeline)
        end_loc = abs_time_to_frame(end_abs, timeline)

        if start_loc is None:
            start_loc = (first_seg, first_frame)
        if end_loc is None:
            end_loc = (last_seg, last_frame)

        phases.append(
            {
                "phase": phase_name,
                "segment_start": start_loc[0],
                "frame_start": start_loc[1],
                "segment_end": end_loc[0],
                "frame_end": end_loc[1],
                "confidence": 0.7,
            }
        )

    return phases


# ------------------------------------------------------------------
# Signal 2: Sonnet vision classification
# ------------------------------------------------------------------


def _detect_with_sonnet(
    manifest: GameManifest,
    task_io: TaskIO,
    timeline: list[dict],
) -> list[dict] | None:
    """Classify game phases by sending sampled tiles to Claude Sonnet.

    Samples center tiles at regular intervals and asks Sonnet to classify
    the game state visible in each frame.
    """
    # Sample frames across the game
    samples = _sample_frames_for_sonnet(manifest, timeline)
    if len(samples) < 4:
        logger.info("Too few samples for Sonnet phase detection (%d)", len(samples))
        return None

    logger.info(
        "Sonnet phase detection: %d samples across %d segments",
        len(samples),
        len(timeline),
    )

    # Pull needed packs for center tile reading
    needed_packs = set()
    for s in samples:
        rows = manifest.conn.execute(
            "SELECT DISTINCT pack_file FROM tiles "
            "WHERE segment = ? AND frame_idx = ? AND pack_file IS NOT NULL",
            (s["segment"], s["frame_idx"]),
        ).fetchall()
        for r in rows:
            needed_packs.add(r[0])

    if needed_packs:
        from training.tasks.sonnet_qa import _pull_selective_packs

        _pull_selective_packs(task_io, needed_packs)

    # Build grids and classify
    classifications = []  # list of (abs_time, segment, frame_idx, phase)

    for grid_start in range(0, len(samples), SONNET_TILES_PER_GRID):
        batch = samples[grid_start : grid_start + SONNET_TILES_PER_GRID]
        result = _classify_grid_with_sonnet(batch, manifest, task_io)

        for sample, phase in zip(batch, result):
            if phase:
                classifications.append(
                    {
                        "abs_time": sample["abs_time"],
                        "segment": sample["segment"],
                        "frame_idx": sample["frame_idx"],
                        "phase": phase,
                    }
                )

    if not classifications:
        logger.info("Sonnet returned no phase classifications")
        return None

    classifications.sort(key=lambda c: c["abs_time"])

    # Smooth: require 2+ consecutive same-phase for a boundary
    return _smooth_classifications(classifications, timeline)


def _sample_frames_for_sonnet(
    manifest: GameManifest, timeline: list[dict]
) -> list[dict]:
    """Sample one frame per SONNET_SAMPLE_INTERVAL_SEC across the game."""
    samples = []

    for seg_info in timeline:
        seg = seg_info["segment"]
        duration = seg_info["end_sec"] - seg_info["start_sec"]
        n_samples = max(1, int(duration / SONNET_SAMPLE_INTERVAL_SEC))

        for i in range(n_samples):
            frac = (i + 0.5) / n_samples  # center of each interval
            frame_idx = int(
                seg_info["frame_min"]
                + frac * (seg_info["frame_max"] - seg_info["frame_min"])
            )
            # Snap to frame interval
            frame_idx = (frame_idx // FRAME_INTERVAL) * FRAME_INTERVAL

            # Verify center tile exists (row=1, col=3)
            tile = manifest.get_tile(seg, frame_idx, 1, 3)
            if not tile or not tile.get("pack_file"):
                # Fallback tiles
                for alt_row, alt_col in [(0, 3), (1, 2), (1, 4)]:
                    tile = manifest.get_tile(seg, frame_idx, alt_row, alt_col)
                    if tile and tile.get("pack_file"):
                        break
                else:
                    continue

            abs_time = frame_to_abs_time(seg, frame_idx, timeline)
            samples.append(
                {
                    "segment": seg,
                    "frame_idx": frame_idx,
                    "abs_time": abs_time,
                    "tile": tile,
                }
            )

    return samples


def _classify_grid_with_sonnet(
    batch: list[dict],
    manifest: GameManifest,
    task_io: TaskIO,
) -> list[str | None]:
    """Build a composite grid and ask Sonnet to classify each tile's game phase.

    Returns list of phase strings (same length as batch), None for failures.
    """
    import cv2

    tile_size = 640
    n = len(batch)
    cols = min(SONNET_GRID_COLS, n)
    rows = (n + cols - 1) // cols

    composite = np.zeros((tile_size * rows, tile_size * cols, 3), dtype=np.uint8)
    valid = [False] * n

    packs_dir = task_io.local_packs

    for idx, sample in enumerate(batch):
        tile = sample["tile"]
        r = idx // cols
        c = idx % cols

        # Read tile from pack
        pack_name = Path(tile["pack_file"]).name
        local_pack = packs_dir / pack_name
        if not local_pack.exists():
            local_pack = Path(tile["pack_file"])
        if not local_pack.exists():
            continue

        try:
            with open(local_pack, "rb") as f:
                f.seek(tile["pack_offset"])
                jpeg_bytes = f.read(tile["pack_size"])

            img_arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
            if img is None:
                continue

            if img.shape[:2] != (tile_size, tile_size):
                img = cv2.resize(img, (tile_size, tile_size))

            y = r * tile_size
            x = c * tile_size
            composite[y : y + tile_size, x : x + tile_size] = img
            valid[idx] = True

            # Add number and timestamp
            abs_min = int(sample["abs_time"] // 60)
            abs_sec = int(sample["abs_time"] % 60)
            label = f"{idx + 1} ({abs_min}:{abs_sec:02d})"
            cv2.putText(
                composite,
                label,
                (x + 10, y + 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.2,
                (0, 255, 0),
                3,
            )
        except Exception as e:
            logger.debug("Failed to read tile for phase grid: %s", e)
            continue

    if not any(valid):
        return [None] * n

    # Save and send to Claude
    grid_path = task_io.local_game / f"phase_grid_{batch[0]['frame_idx']}.jpg"
    cv2.imwrite(str(grid_path), composite, [cv2.IMWRITE_JPEG_QUALITY, 85])

    n_valid = sum(valid)
    prompt = (
        f"Read the image at {grid_path} and analyze it. "
        f"This image shows a grid of {n_valid} numbered soccer field views from a "
        f"panoramic camera, taken at different times during a game recording. "
        f"Each tile shows the center of the field. The timestamp in parentheses "
        f"shows the wall-clock time.\n\n"
        f"For each numbered tile (1-{n}), classify the game phase:\n"
        f"- PRE_GAME: warmup — multiple scattered balls, players warming up, "
        f"no organized formation, may see goalkeepers practicing\n"
        f"- FIRST_HALF: active play — players in organized positions, game in "
        f"progress, one game ball, referee visible\n"
        f"- HALFTIME: break between halves — field may be empty or have scattered "
        f"groups, no active play, possibly people walking around\n"
        f"- SECOND_HALF: active play resumed — same as FIRST_HALF but later in recording\n"
        f"- POST_GAME: game ended — players leaving, handshakes, scattered equipment\n\n"
        f"Respond with ONLY a JSON object mapping tile number to phase. Example:\n"
        f'{{"1": "PRE_GAME", "2": "FIRST_HALF", "3": "FIRST_HALF", "4": "HALFTIME"}}'
    )

    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                "sonnet",
                "--allowedTools",
                "Read",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.warning(
                "Phase detection Claude call failed (rc=%d): %s",
                result.returncode,
                result.stderr[:200],
            )
            return [None] * n

        from training.tasks.sonnet_qa import _extract_json

        data = _extract_json(result.stdout.strip())
        if not data:
            logger.warning("Could not parse phase detection response")
            return [None] * n

        # Map numbered results back to phases
        phases = []
        valid_phases = {
            "PRE_GAME",
            "FIRST_HALF",
            "HALFTIME",
            "SECOND_HALF",
            "POST_GAME",
        }
        for i in range(n):
            key = str(i + 1)
            phase = data.get(key, "")
            if isinstance(phase, str):
                phase = phase.upper().strip()
            if phase in valid_phases:
                phases.append(phase.lower())
            else:
                phases.append(None)

        return phases

    except subprocess.TimeoutExpired:
        logger.warning("Phase detection Claude call timed out")
        return [None] * n
    except Exception as e:
        logger.warning("Phase detection Claude call error: %s", e)
        return [None] * n
    finally:
        time.sleep(2)  # rate limit


def _smooth_classifications(
    classifications: list[dict], timeline: list[dict]
) -> list[dict]:
    """Smooth noisy per-frame classifications into contiguous phase regions.

    Requires 2+ consecutive same-phase classifications to establish a boundary.
    """
    if not classifications:
        return []

    # Group consecutive same-phase runs
    runs = []
    current_phase = classifications[0]["phase"]
    run_start = 0

    for i in range(1, len(classifications)):
        if classifications[i]["phase"] != current_phase:
            runs.append(
                {
                    "phase": current_phase,
                    "start_idx": run_start,
                    "end_idx": i - 1,
                    "count": i - run_start,
                }
            )
            current_phase = classifications[i]["phase"]
            run_start = i

    runs.append(
        {
            "phase": current_phase,
            "start_idx": run_start,
            "end_idx": len(classifications) - 1,
            "count": len(classifications) - run_start,
        }
    )

    # Filter out short runs (noise) — require at least 2 consecutive classifications
    stable_runs = [r for r in runs if r["count"] >= 2]

    if not stable_runs:
        # Fall back to longest run
        stable_runs = [max(runs, key=lambda r: r["count"])]

    # Build phase entries from stable runs
    first_seg = timeline[0]["segment"]
    first_frame = timeline[0]["frame_min"]
    last_seg = timeline[-1]["segment"]
    last_frame = timeline[-1]["frame_max"]

    phases = []
    for i, run in enumerate(stable_runs):
        start_cls = classifications[run["start_idx"]]
        end_cls = classifications[run["end_idx"]]

        # Extend phase to cover gap before next phase
        if i == 0:
            seg_start = first_seg
            frame_start = first_frame
        else:
            seg_start = start_cls["segment"]
            frame_start = start_cls["frame_idx"]

        if i == len(stable_runs) - 1:
            seg_end = last_seg
            frame_end = last_frame
        else:
            seg_end = end_cls["segment"]
            frame_end = end_cls["frame_idx"]

        phases.append(
            {
                "phase": run["phase"],
                "segment_start": seg_start,
                "frame_start": frame_start,
                "segment_end": seg_end,
                "frame_end": frame_end,
                "confidence": min(0.9, 0.5 + run["count"] * 0.1),
            }
        )

    return phases


# ------------------------------------------------------------------
# Merge signals
# ------------------------------------------------------------------


def _merge_phase_signals(
    whistle_phases: list[dict] | None,
    sonnet_phases: list[dict] | None,
    timeline: list[dict],
) -> list[dict]:
    """Merge whistle and Sonnet phase detections.

    Whistle takes priority (precise boundaries). Sonnet fills gaps.
    """
    if whistle_phases:
        return whistle_phases

    if sonnet_phases:
        return sonnet_phases

    return []
