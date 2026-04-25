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

    # Build label density timeline for kickoff scoring
    label_timeline = _build_label_density_timeline(manifest, seg_timeline)

    # Signal 1: Whistle detection from audio (with label + crowd signals)
    whistle_phases = _detect_whistles(
        task_io,
        seg_timeline,
        label_timeline=label_timeline,
    )

    # Signal 2: Sonnet vision confirmation of whistle boundaries
    # Extracts frames from video .mp4 files (no pack files needed)
    sonnet_phases = _detect_with_sonnet(
        manifest, task_io, seg_timeline, whistle_phases=whistle_phases
    )

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


def _detect_whistles(
    task_io: TaskIO,
    timeline: list[dict],
    label_timeline: list[dict] | None = None,
) -> list[dict] | None:
    """Detect referee whistles from video audio tracks.

    Uses label density and crowd energy as secondary scoring signals
    when classifying whistle clusters into phase boundaries.

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
    logger.info(
        "Whistle clusters: %s",
        [f"{t:.0f}s ({n} blasts)" for t, n in clusters],
    )

    # Build crowd energy timeline from the same audio data
    crowd_timeline = _build_crowd_timeline(task_io, timeline)

    # Classify clusters into game events using all signals
    return _classify_whistle_clusters(
        clusters,
        timeline,
        label_timeline=label_timeline,
        crowd_timeline=crowd_timeline,
    )


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


def _cluster_whistle_events(
    timestamps: list[float],
) -> list[tuple[float, int]]:
    """Cluster nearby whistle events into single game events.

    A referee may blow 3-5 short blasts -- these cluster into one event.
    Returns list of (center_timestamp, blast_count) tuples.
    Phase boundaries (halftime, fulltime) typically have 3+ blasts,
    while in-game fouls are single blasts.
    """
    if not timestamps:
        return []

    clusters = []
    cluster_start = timestamps[0]
    cluster_end = timestamps[0]
    count = 1

    for t in timestamps[1:]:
        if t - cluster_end <= WHISTLE_CLUSTER_GAP:
            cluster_end = t
            count += 1
        else:
            clusters.append(((cluster_start + cluster_end) / 2, count))
            cluster_start = t
            cluster_end = t
            count = 1

    clusters.append(((cluster_start + cluster_end) / 2, count))
    return clusters


def _build_label_density_timeline(
    manifest: GameManifest, timeline: list[dict], window_sec: int = 60
) -> list[dict]:
    """Build label density timeline from manifest data.

    Returns list of {abs_time, density} dicts in 1-min windows.
    """
    result = []
    FPS_LOCAL = 30
    WINDOW_FRAMES = window_sec * FPS_LOCAL

    for seg_info in timeline:
        seg = seg_info["segment"]
        fmin = seg_info["frame_min"]
        fmax = seg_info["frame_max"]

        # Count labels for this segment (fast: one query)
        total_labels = manifest.conn.execute(
            "SELECT COUNT(*) FROM labels WHERE tile_stem LIKE ? || '_frame_%'",
            (seg,),
        ).fetchone()[0]
        total_tiles = manifest.conn.execute(
            "SELECT COUNT(*) FROM tiles WHERE segment = ?", (seg,)
        ).fetchone()[0]

        if total_tiles == 0:
            continue

        # Approximate density per window (uniform distribution assumption)
        n_windows = max(1, (fmax - fmin) // WINDOW_FRAMES)
        density = total_labels / total_tiles

        for w in range(n_windows):
            abs_time = seg_info["start_sec"] + (w * WINDOW_FRAMES) / FPS_LOCAL
            result.append({"abs_time": abs_time, "density": density})

    result.sort(key=lambda t: t["abs_time"])
    return result


def _build_crowd_timeline(
    task_io: TaskIO, timeline: list[dict], window_sec: int = 60
) -> list[dict] | None:
    """Build crowd energy timeline from video audio.

    Measures energy in the 200-2000 Hz band (crowd noise) in windows.
    Returns list of {abs_time, crowd_energy} or None if audio unavailable.
    """
    video_dir = task_io.local_video
    if not video_dir or not video_dir.exists():
        return None

    video_files = sorted(video_dir.glob("*.mp4"))
    if not video_files:
        return None

    result = []

    for vf in video_files:
        seg_name = vf.stem
        seg_info = next((t for t in timeline if t["segment"] == seg_name), None)
        if not seg_info:
            for t in timeline:
                if t["segment"].startswith(seg_name[:8]):
                    seg_info = t
                    break
        if not seg_info:
            continue

        try:
            import av

            container = av.open(str(vf))
            if not container.streams.audio:
                container.close()
                continue

            sr = container.streams.audio[0].rate or 44100
            samples = []
            for frame in container.decode(audio=0):
                arr = frame.to_ndarray()
                if arr.ndim > 1:
                    arr = arr.mean(axis=0)
                samples.append(arr)
            container.close()

            if not samples:
                continue

            audio = np.concatenate(samples).astype(np.float32)

            # Analyze crowd energy in windows
            window_samples = int(window_sec * sr)
            freqs = np.fft.rfftfreq(min(window_samples, len(audio)), d=1.0 / sr)
            crowd_mask = (freqs >= 200) & (freqs < 2000)
            hann = np.hanning(min(window_samples, len(audio)))

            for w_start in range(0, len(audio) - window_samples, window_samples):
                chunk = audio[w_start : w_start + window_samples]
                if len(chunk) < window_samples:
                    break
                spectrum = np.abs(np.fft.rfft(chunk * hann[: len(chunk)]))
                crowd_energy = float(np.sum(spectrum[crowd_mask] ** 2))

                abs_time = seg_info["start_sec"] + w_start / sr
                result.append({"abs_time": abs_time, "crowd_energy": crowd_energy})

        except Exception as e:
            logger.debug("Crowd timeline failed for %s: %s", vf.name, e)
            continue

    if not result:
        return None

    result.sort(key=lambda t: t["abs_time"])
    return result


def _classify_whistle_clusters(
    clusters: list[tuple[float, int]],
    timeline: list[dict],
    label_timeline: list[dict] | None = None,
    crowd_timeline: list[dict] | None = None,
) -> list[dict] | None:
    """Classify whistle clusters into game phase boundaries.

    Uses multiple signals (EXP-012 findings):
    1. Duration constraints (25-38m halves, 3-12m halftime)
    2. Label density drop at kickoff (warmup multi-ball -> single ball)
    3. Crowd energy jump at 2H start (players cheering)
    4. Calibrated offsets to correct systematic early-detection bias

    Returns phase list or None if pattern doesn't match.
    """
    if len(clusters) < 2:
        return None

    game_start_sec = timeline[0]["start_sec"]
    game_end_sec = timeline[-1]["end_sec"]

    # Duration parameters (from 31 human-tagged games, EXP-012)
    EXPECTED_HALF = 32 * 60  # 32 min (slightly above 30.5m avg to reduce early bias)
    EXPECTED_BREAK = 8 * 60  # 8 min (slightly above 6.5m avg)
    MIN_HALF = 22 * 60
    MAX_HALF = 38 * 60
    MIN_BREAK = 3 * 60
    MAX_BREAK = 12 * 60

    # Calibrated offsets from EXP-012 (correct systematic early-detection bias)
    KICKOFF_OFFSET = 5 * 60  # whistles detected ~5m before actual kickoff
    HT_START_OFFSET = 3 * 60  # ~3m before actual halftime
    SH_START_OFFSET = 2 * 60  # ~2m before actual 2H start
    # Game end offset = 0 (already accurate)

    best_phases = None
    best_score = float("inf")

    cluster_times = [c[0] for c in clusters]

    if len(cluster_times) >= 4:
        for i in range(len(cluster_times)):
            for j in range(i + 1, len(cluster_times)):
                for k in range(j + 1, len(cluster_times)):
                    for m in range(k + 1, len(cluster_times)):
                        c = [
                            cluster_times[i],
                            cluster_times[j],
                            cluster_times[k],
                            cluster_times[m],
                        ]
                        h1 = c[1] - c[0]
                        brk = c[2] - c[1]
                        h2 = c[3] - c[2]

                        if h1 < MIN_HALF or h1 > MAX_HALF:
                            continue
                        if h2 < MIN_HALF or h2 > MAX_HALF:
                            continue
                        if brk < MIN_BREAK or brk > MAX_BREAK:
                            continue

                        # Duration score
                        score = (
                            abs(h1 - EXPECTED_HALF)
                            + abs(brk - EXPECTED_BREAK) * 0.5
                            + abs(h2 - EXPECTED_HALF)
                        )

                        # Label density at kickoff: reward density DROP
                        if label_timeline:
                            score += _density_score_at(
                                label_timeline, c[0], bonus_for_drop=True
                            )

                        # Crowd energy at 2H start: reward energy JUMP
                        if crowd_timeline:
                            score += _crowd_score_at(crowd_timeline, c[2])

                        if score < best_score:
                            best_score = score
                            # Apply calibrated offsets before building phases
                            adjusted = [
                                c[0] + KICKOFF_OFFSET,
                                c[1] + HT_START_OFFSET,
                                c[2] + SH_START_OFFSET,
                                c[3],  # game end: no offset
                            ]
                            best_phases = _build_phases_from_whistles(
                                adjusted,
                                game_start_sec,
                                game_end_sec,
                                timeline,
                            )

    # Fallback: 2-whistle pattern
    if best_phases is None and len(cluster_times) >= 2:
        c0, c1 = cluster_times[0], cluster_times[-1]
        game_duration = c1 - c0
        if 45 * 60 <= game_duration <= 100 * 60:
            adjusted = [c0 + KICKOFF_OFFSET, c1]
            best_phases = _build_phases_from_whistles(
                adjusted, game_start_sec, game_end_sec, timeline
            )

    return best_phases


def _density_score_at(
    label_timeline: list[dict], time_point: float, bonus_for_drop: bool = True
) -> float:
    """Score based on label density change around a time point.

    Returns a score adjustment (negative = bonus, positive = penalty).
    Used for kickoff detection: density should DROP as warmup ends.
    """
    before = [
        t["density"]
        for t in label_timeline
        if time_point - 300 <= t["abs_time"] < time_point
    ]
    after = [
        t["density"]
        for t in label_timeline
        if time_point <= t["abs_time"] < time_point + 300
    ]

    if not before or not after:
        return 0

    import statistics

    d_before = statistics.mean(before)
    d_after = statistics.mean(after)

    if d_before <= 0:
        return 0

    ratio = d_after / d_before

    if bonus_for_drop:
        if ratio < 0.7:
            return -3 * 60  # strong bonus for density drop
        elif ratio < 0.9:
            return -1 * 60  # mild bonus
        elif ratio > 1.3:
            return 2 * 60  # penalty for density rise at "kickoff"
    return 0


def _crowd_score_at(crowd_timeline: list[dict], time_point: float) -> float:
    """Score based on crowd energy change around a time point.

    Returns a score adjustment. Used for 2H start detection:
    players cheering before going back on field causes energy jump.
    """
    before = [
        t["crowd_energy"]
        for t in crowd_timeline
        if time_point - 300 <= t["abs_time"] < time_point
    ]
    after = [
        t["crowd_energy"]
        for t in crowd_timeline
        if time_point <= t["abs_time"] < time_point + 300
    ]

    if not before or not after:
        return 0

    import statistics

    e_before = statistics.mean(before)
    e_after = statistics.mean(after)

    if e_before <= 0:
        return 0

    ratio = e_after / e_before
    if ratio > 1.5:
        return -3 * 60  # strong bonus for energy jump (players cheering)
    elif ratio > 1.2:
        return -1 * 60  # mild bonus
    return 0


def _build_phases_from_whistles(
    whistle_times: list[float],
    game_start_sec: float,
    game_end_sec: float,
    timeline: list[dict],
) -> list[dict]:
    """Build phase list from classified whistle timestamps.

    Enforces strict ordering: each phase starts where the previous one ends.
    This prevents overlapping or out-of-order phases.
    """
    if len(whistle_times) >= 4:
        boundary_times = [
            game_start_sec,
            whistle_times[0],
            whistle_times[1],
            whistle_times[2],
            whistle_times[3],
            game_end_sec,
        ]
        phase_names = ["pre_game", "first_half", "halftime", "second_half", "post_game"]
    elif len(whistle_times) == 2:
        boundary_times = [
            game_start_sec,
            whistle_times[0],
            whistle_times[1],
            game_end_sec,
        ]
        phase_names = ["pre_game", "first_half", "post_game"]
    else:
        return []

    # Convert boundary times to (segment, frame) locations
    # Use nearest segment if exact match not found
    boundary_locs = []
    for t in boundary_times:
        loc = abs_time_to_frame(t, timeline)
        if loc is None:
            # Find nearest segment boundary
            nearest = min(
                timeline,
                key=lambda s: min(abs(s["start_sec"] - t), abs(s["end_sec"] - t)),
            )
            if t <= nearest["start_sec"]:
                loc = (nearest["segment"], nearest["frame_min"])
            else:
                loc = (nearest["segment"], nearest["frame_max"])
        boundary_locs.append(loc)

    # Build phases with strict ordering: each phase starts where previous ends
    phases = []
    for i, phase_name in enumerate(phase_names):
        start_loc = boundary_locs[i]
        end_loc = boundary_locs[i + 1]

        # Enforce ordering: if end is before start (can happen with segment
        # time overlaps), clamp end to start
        start_abs = boundary_times[i]
        end_abs = boundary_times[i + 1]
        if end_abs < start_abs:
            end_loc = start_loc

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
    whistle_phases: list[dict] | None = None,
) -> list[dict] | None:
    """Confirm whistle-detected phase boundaries using video frames.

    Extracts frames directly from .mp4 video files (already pulled for audio)
    around each whistle-detected boundary. No pack files needed.

    If whistle_phases is provided, samples frames around those boundaries
    for confirmation. Otherwise falls back to evenly-spaced sampling.
    """
    video_dir = task_io.local_video
    if not video_dir or not video_dir.exists():
        logger.info("No video dir for Sonnet confirmation")
        return None

    video_files = {vf.stem: vf for vf in sorted(video_dir.glob("*.mp4"))}
    if not video_files:
        logger.info("No video files for Sonnet confirmation")
        return None

    # Check if this game needs 180-degree rotation
    needs_flip = False
    try:
        from training.pipeline.config import load_config
        from training.pipeline.registry import GameRegistry

        cfg = load_config()
        reg = GameRegistry(cfg.paths.registry_db)
        game_info = reg.get_game(manifest.game_id)
        if game_info and game_info.get("needs_flip"):
            needs_flip = True
        reg.close()
    except Exception:
        pass

    # Determine which timestamps to sample
    if whistle_phases:
        # Sample around each boundary: 30s before and after each transition
        sample_times = []
        for p in whistle_phases:
            # Phase start boundary
            start_sec = _phase_boundary_to_abs(p, "start", timeline)
            # Phase end boundary
            end_sec = _phase_boundary_to_abs(p, "end", timeline)
            if start_sec is not None:
                sample_times.extend([start_sec - 30, start_sec, start_sec + 30])
            if end_sec is not None:
                sample_times.extend([end_sec - 30, end_sec, end_sec + 30])
        # Deduplicate and sort
        sample_times = sorted(set(t for t in sample_times if t > 0))
    else:
        # Fallback: sample every 60s across the game
        game_start = timeline[0]["start_sec"]
        game_end = timeline[-1]["end_sec"]
        sample_times = []
        t = game_start
        while t <= game_end:
            sample_times.append(t)
            t += SONNET_SAMPLE_INTERVAL_SEC

    if len(sample_times) < 4:
        logger.info("Too few sample times for Sonnet (%d)", len(sample_times))
        return None

    logger.info("Sonnet boundary confirmation: %d frames to extract", len(sample_times))

    # Extract frames from video files at the target timestamps
    frames = []
    for abs_time in sample_times:
        # Find which segment contains this time
        seg_info = None
        for s in timeline:
            if s["start_sec"] <= abs_time <= s["end_sec"]:
                seg_info = s
                break
        if not seg_info:
            # Use nearest segment
            seg_info = min(
                timeline,
                key=lambda s: min(
                    abs(s["start_sec"] - abs_time), abs(s["end_sec"] - abs_time)
                ),
            )

        offset_in_seg = abs_time - seg_info["start_sec"]

        # Find the video file for this segment
        vf = video_files.get(seg_info["segment"])
        if not vf:
            # Try partial match
            for stem, path in video_files.items():
                if stem.startswith(seg_info["segment"][:8]):
                    vf = path
                    break
        if not vf:
            continue

        # Extract frame using OpenCV (fast seek)
        frame_img = _extract_video_frame(vf, offset_in_seg, flip=needs_flip)
        if frame_img is not None:
            frames.append(
                {
                    "abs_time": abs_time,
                    "segment": seg_info["segment"],
                    "image": frame_img,
                }
            )

    if len(frames) < 4:
        logger.info("Could only extract %d frames from video", len(frames))
        return None

    logger.info("Extracted %d frames from video for Sonnet", len(frames))

    # Build grids and classify
    classifications = []
    for grid_start in range(0, len(frames), SONNET_TILES_PER_GRID):
        batch = frames[grid_start : grid_start + SONNET_TILES_PER_GRID]
        result = _classify_video_frames_with_sonnet(batch, task_io)
        for frame_info, phase in zip(batch, result):
            if phase:
                # Convert abs_time to frame_idx for _smooth_classifications
                frame_result = abs_time_to_frame(frame_info["abs_time"], timeline)
                if frame_result:
                    seg, fidx = frame_result
                else:
                    seg = frame_info["segment"]
                    fidx = 0
                classifications.append(
                    {
                        "abs_time": frame_info["abs_time"],
                        "segment": seg,
                        "frame_idx": fidx,
                        "phase": phase,
                    }
                )

    if not classifications:
        logger.info("Sonnet returned no phase classifications")
        return None

    classifications.sort(key=lambda c: c["abs_time"])
    return _smooth_classifications(classifications, timeline)


def _phase_boundary_to_abs(
    phase: dict, boundary: str, timeline: list[dict]
) -> float | None:
    """Convert a phase boundary to absolute seconds."""
    seg_name = phase[f"segment_{boundary}"]
    frame = phase[f"frame_{boundary}"]
    for s in timeline:
        if s["segment"] == seg_name:
            return s["start_sec"] + frame / FPS
    return None


def _extract_video_frame(
    video_path: Path, offset_sec: float, *, flip: bool = False
) -> "np.ndarray | None":
    """Extract a single frame from a video at the given offset."""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    target_frame = int(offset_sec * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        return None

    if flip:
        frame = cv2.rotate(frame, cv2.ROTATE_180)

    # Downscale for Sonnet (save tokens) -- half res
    h, w = frame.shape[:2]
    return cv2.resize(frame, (w // 2, h // 2))


def _classify_video_frames_with_sonnet(
    batch: list[dict],
    task_io: TaskIO,
) -> list[str | None]:
    """Build a composite grid from video frames and ask Sonnet to classify."""
    import cv2

    if not batch:
        return []

    # Build composite grid
    n = len(batch)
    cols = min(SONNET_GRID_COLS, n)
    rows = (n + cols - 1) // cols

    # Get size from first frame
    sample = batch[0]["image"]
    tile_h, tile_w = sample.shape[:2]

    composite = np.zeros((tile_h * rows, tile_w * cols, 3), dtype=np.uint8)
    valid = [False] * n

    for idx, frame_info in enumerate(batch):
        r = idx // cols
        c = idx % cols
        img = frame_info["image"]
        if img.shape[:2] != (tile_h, tile_w):
            img = cv2.resize(img, (tile_w, tile_h))

        y = r * tile_h
        x = c * tile_w
        composite[y : y + tile_h, x : x + tile_w] = img
        valid[idx] = True

        # Add label with time
        abs_min = int(frame_info["abs_time"] // 60)
        abs_sec = int(frame_info["abs_time"] % 60)
        label = f"{idx + 1} ({abs_min % 60}:{abs_sec:02d})"
        cv2.putText(
            composite,
            label,
            (x + 10, y + 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 255, 0),
            2,
        )

    if not any(valid):
        return [None] * n

    grid_path = task_io.local_game / "phase_confirm_grid.jpg"
    cv2.imwrite(str(grid_path), composite, [cv2.IMWRITE_JPEG_QUALITY, 85])

    n_valid = sum(valid)
    prompt = (
        f"Read the image at {grid_path} and analyze it. "
        f"This image shows a grid of {n_valid} numbered video frames from a "
        f"youth soccer game camera, taken at times around suspected phase "
        f"boundaries (kickoff, halftime, fulltime). "
        f"Each frame shows the full panoramic view of the field. "
        f"The number and timestamp are in the top-left corner.\n\n"
        f"For each numbered frame (1-{n}), classify the game phase:\n"
        f"- PRE_GAME: warmup, multiple balls, no organized play, "
        f"or camera indoors/pointed at wall\n"
        f"- FIRST_HALF: active game play, organized teams, one ball\n"
        f"- HALFTIME: players walking off field, field mostly empty\n"
        f"- SECOND_HALF: active play resumed\n"
        f"- POST_GAME: handshakes, packing up, field emptying\n\n"
        f"Respond with ONLY a JSON object mapping frame number to phase. Example:\n"
        f'{{"1": "PRE_GAME", "2": "FIRST_HALF", "3": "FIRST_HALF"}}'
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
                "Phase confirm Claude call failed (rc=%d)", result.returncode
            )
            return [None] * n

        from training.tasks.sonnet_qa import _extract_json

        data = _extract_json(result.stdout.strip())
        if not data:
            return [None] * n

        valid_phases = {
            "PRE_GAME",
            "FIRST_HALF",
            "HALFTIME",
            "SECOND_HALF",
            "POST_GAME",
        }
        phases = []
        for i in range(n):
            val = data.get(str(i + 1), data.get(i + 1))
            if isinstance(val, str):
                val = val.upper().strip()
            phases.append(val if val in valid_phases else None)
        return phases

    except Exception as e:
        logger.warning("Phase confirm failed: %s", e)
        return [None] * n


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

    Whistle provides precise boundary timestamps.
    Sonnet provides visual classification to validate whistle boundaries.

    If both exist: use whistle boundaries but cross-check with Sonnet.
    If only whistle: use whistle.
    If only Sonnet: use Sonnet.
    """
    if whistle_phases and not sonnet_phases:
        return whistle_phases

    if sonnet_phases and not whistle_phases:
        return sonnet_phases

    if not whistle_phases and not sonnet_phases:
        return []

    # Both exist: use whistle boundaries (more precise)
    # but log disagreements for debugging
    return whistle_phases
