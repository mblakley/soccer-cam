"""Generate field-boundary keypoint labels by running the teacher model.

Walks recording-group directories, routes each game to a team via its
``match_info.ini``, samples frames at a fixed interval, runs the teacher
keypoint model on each, and writes one JSON label per frame (plus a stored
JPEG, and optional overlay PNGs for eyeballing). The result is a distilled
training set the student model is fitted against.

The teacher path is a required argument with no default — no teacher
identity lives in the repo.

Usage::

    # Dry run: just print the team-routing table (confirm zero UNKNOWN)
    python -m training.cli.generate_field_labels \\
        --roots F:/Soccer_Archive/reolink/Heat F:/Soccer_Archive/reolink/Flash \\
        --output-root F:/training_data/field_keypoints --dry-run

    # Full label generation
    python -m training.cli.generate_field_labels \\
        --roots F:/Soccer_Archive/reolink/Heat F:/Soccer_Archive/reolink/Flash \\
        --teacher-model <teacher.onnx> \\
        --output-root F:/training_data/field_keypoints \\
        --interval-sec 60 --max-per-group 150 --render-overlays
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from training.field_keypoints import (
    GATE_THRESHOLD,
    LABEL_SCHEMA_VERSION,
    NUM_KEYPOINTS,
    make_game_id,
    slugify,
    team_from_name,
)
from video_grouper.models.match_info import MatchInfo

logger = logging.getLogger(__name__)

# Frames are stored downscaled to this max width (aspect preserved). 1920 is
# ~2.5x the 768px training width — enough headroom for crop/zoom augmentation
# at a fraction of full-resolution storage.
STORE_MAX_WIDTH = 1920
JPEG_QUALITY = 90

# Files that are not game footage even though they end in .mp4.
_SKIP_VIDEO_PREFIXES = ("temp_", "thumb_")
_SKIP_VIDEO_SUFFIXES = (".tmp",)


@dataclass
class GameSpec:
    """One recording group resolved to a game, team and its video files."""

    group_dir: Path
    game_id: str
    team: str | None
    opponent: str
    venue: str
    date: str
    my_team_name: str
    videos: list[Path] = field(default_factory=list)
    unknown_reason: str | None = None


# ---------------------------------------------------------------------------
# Discovery + team routing
# ---------------------------------------------------------------------------


def _select_videos(group_dir: Path, use_segments: bool) -> list[Path]:
    """Pick the video file(s) to sample from a recording group.

    Prefers a single ``combined.mp4`` (already stitched + deduped); falls
    back to the raw segment ``.mp4`` files sorted by name.
    """
    combined = group_dir / "combined.mp4"
    if combined.exists() and not use_segments:
        return [combined]

    segments: list[Path] = []
    for p in sorted(group_dir.glob("*.mp4")):
        name = p.name.lower()
        if name.startswith("combined"):
            continue
        if any(name.startswith(pre) for pre in _SKIP_VIDEO_PREFIXES):
            continue
        if any(name.endswith(suf) for suf in _SKIP_VIDEO_SUFFIXES):
            continue
        segments.append(p)
    return segments


def discover_games(
    roots: list[Path], groups_glob: str | None, use_segments: bool
) -> list[GameSpec]:
    """Find recording groups under ``roots`` and resolve team/venue/videos."""
    games: list[GameSpec] = []
    seen_ids: dict[str, Path] = {}

    for root in roots:
        if not root.exists():
            logger.warning("Root does not exist: %s", root)
            continue
        for group_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if groups_glob and not fnmatch.fnmatch(group_dir.name, groups_glob):
                continue

            videos = _select_videos(group_dir, use_segments)
            if not videos:
                continue  # not a footage group

            mi_path = group_dir / "match_info.ini"
            mi = MatchInfo.from_file(str(mi_path)) if mi_path.exists() else None
            my_team = mi.my_team_name if mi else ""
            opponent = mi.opponent_team_name if mi else ""
            venue = mi.location if mi else ""
            date = group_dir.name[:10]  # YYYY.MM.DD prefix of the group dir
            team = team_from_name(my_team)

            if team is None:
                games.append(
                    GameSpec(
                        group_dir=group_dir,
                        game_id=f"unknown__{slugify(group_dir.name)}",
                        team=None,
                        opponent=opponent,
                        venue=venue,
                        date=date,
                        my_team_name=my_team,
                        videos=videos,
                        unknown_reason=(
                            "no_match_info"
                            if mi is None
                            else f"unrecognized_team:{my_team!r}"
                        ),
                    )
                )
                continue

            game_id = make_game_id(team, date, opponent, venue)
            # Disambiguate same-day/same-venue repeats by appending the time.
            if game_id in seen_ids and seen_ids[game_id] != group_dir:
                game_id = f"{game_id}_{slugify(group_dir.name[11:])}"
            seen_ids[game_id] = group_dir

            games.append(
                GameSpec(
                    group_dir=group_dir,
                    game_id=game_id,
                    team=team,
                    opponent=opponent,
                    venue=venue,
                    date=date,
                    my_team_name=my_team,
                    videos=videos,
                )
            )
    return games


def print_routing_table(games: list[GameSpec]) -> int:
    """Print the routing table. Returns the count of *blocking* unknowns.

    A dir with no ``match_info.ini`` isn't a game (warmup clip, ``_clips``,
    etc.) and is skipped silently — not counted as blocking. A dir that *has*
    match_info but an unrecognized team is a real game that must be fixed, so
    it blocks unless ``--allow-unknown`` is passed.
    """
    by_team: dict[str, int] = {"flash": 0, "heat": 0}
    routed = [g for g in games if g.team is not None]
    no_mi = [g for g in games if g.team is None and g.unknown_reason == "no_match_info"]
    bad_team = [
        g for g in games if g.team is None and g.unknown_reason != "no_match_info"
    ]

    print(f"\n{'TEAM':6} {'VENUE':28} {'DATE':11} GROUP")
    print("-" * 90)
    for g in sorted(routed, key=lambda g: (g.team, g.date)):
        by_team[g.team] = by_team.get(g.team, 0) + 1
        print(f"{g.team.upper():6} {g.venue[:28]:28} {g.date:11} {g.group_dir.name}")
    for g in sorted(bad_team, key=lambda g: g.date):
        print(
            f"{'BADTEAM':6} {g.venue[:28]:28} {g.date:11} {g.group_dir.name}  <- {g.unknown_reason}"
        )
    print("-" * 90)
    print(
        f"flash={by_team.get('flash', 0)}  heat={by_team.get('heat', 0)}  "
        f"unrecognized_team={len(bad_team)}  skipped_non_games={len(no_mi)}  "
        f"total={len(games)}\n"
    )
    return len(bad_team)


# ---------------------------------------------------------------------------
# Frame sampling (PyAV, seek-based to avoid full decode)
# ---------------------------------------------------------------------------


def sample_frames(video_path: Path, interval_sec: float, max_frames: int):
    """Yield ``(frame_bgr, t_sec)`` sampled ~every ``interval_sec`` seconds.

    Uses keyframe seeking so we decode only a handful of frames per long
    segment instead of the whole file. The exact frame at ``t`` is not
    important for a static field boundary, so we take the first frame
    decoded after each seek.
    """
    import av  # local import: keeps onnxruntime/torch-free importers happy

    container = av.open(str(video_path))
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"

        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            duration = container.duration / av.time_base
        else:
            duration = 0.0
        if duration <= 0:
            logger.warning("Unknown duration for %s; sampling start only", video_path)
            duration = interval_sec

        times = []
        t = 0.0
        while t < duration and len(times) < max_frames:
            times.append(t)
            t += interval_sec

        for t_sec in times:
            try:
                offset = int(t_sec / stream.time_base) if stream.time_base else 0
                container.seek(offset, stream=stream, backward=True)
                frame = next(container.decode(stream), None)
            except Exception as e:  # corrupt packet at this offset — skip it
                logger.debug(
                    "Seek/decode failed at %.0fs in %s: %s", t_sec, video_path, e
                )
                continue
            if frame is None:
                continue
            yield frame.to_ndarray(format="bgr24"), t_sec
    finally:
        container.close()


# ---------------------------------------------------------------------------
# Teacher inference + label writing
# ---------------------------------------------------------------------------


def _store_frame(frame_bgr: np.ndarray, out_path: Path) -> tuple[int, int]:
    """Write a downscaled JPEG; return ``(stored_w, stored_h)``."""
    h, w = frame_bgr.shape[:2]
    if w > STORE_MAX_WIDTH:
        scale = STORE_MAX_WIDTH / w
        out = cv2.resize(frame_bgr, (STORE_MAX_WIDTH, round(h * scale)))
    else:
        out = frame_bgr
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), out, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return out.shape[1], out.shape[0]


def _draw_overlay(
    frame_bgr: np.ndarray, kpts_norm: list[list[float]], scores: list[float]
) -> np.ndarray:
    """Draw the teacher polygon + numbered keypoints for visual QA."""
    img = frame_bgr.copy()
    h, w = img.shape[:2]
    pts = [(int(x * w), int(y * h)) for x, y in kpts_norm]
    near, far = pts[:5], pts[5:]
    cv2.polylines(img, [np.array(near, np.int32)], False, (0, 255, 0), 2)
    cv2.polylines(img, [np.array(far, np.int32)], False, (255, 255, 0), 2)
    for i, (px, py) in enumerate(pts):
        color = (0, 255, 0) if i < 5 else (255, 255, 0)
        cv2.circle(img, (px, py), 5, color, -1)
        cv2.putText(
            img,
            f"{i}:{scores[i]:.2f}",
            (px + 6, py),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
            cv2.LINE_AA,
        )
    return img


def process_game(spec: GameSpec, sess, args, teacher_sha: str) -> dict:
    """Sample, run the teacher, and write labels for one game."""
    from video_grouper.inference.field_detector import detect_field_keypoints

    frames_dir = args.output_root / "frames" / spec.game_id
    labels_dir = args.output_root / "labels" / spec.game_id
    overlays_dir = args.output_root / "overlays" / spec.game_id

    written = skipped = failed = 0
    score_sum = 0.0

    per_video_cap = args.max_per_segment
    remaining_for_group = args.max_per_group

    for video in spec.videos:
        if remaining_for_group <= 0:
            break
        cap = min(per_video_cap, remaining_for_group)
        try:
            for frame_bgr, t_sec in sample_frames(video, args.interval_sec, cap):
                stem = f"{video.stem}_t{int(t_sec):06d}"
                jpg_path = frames_dir / f"{stem}.jpg"
                json_path = labels_dir / f"{stem}.json"

                if not args.force and jpg_path.exists() and json_path.exists():
                    skipped += 1
                    remaining_for_group -= 1
                    continue

                try:
                    orig_h, orig_w = frame_bgr.shape[:2]
                    kpts = detect_field_keypoints(frame_bgr, sess, score_threshold=0.0)
                    # threshold 0.0 => every point returned with coords + score
                    kpts_norm = [
                        [float(kp[0]) / orig_w, float(kp[1]) / orig_h] for kp in kpts
                    ]
                    scores = [float(kp[2]) for kp in kpts]
                    mean_score = sum(scores) / len(scores)

                    stored_w, stored_h = _store_frame(frame_bgr, jpg_path)
                    label = {
                        "schema_version": LABEL_SCHEMA_VERSION,
                        "game_id": spec.game_id,
                        "team": spec.team,
                        "venue": spec.venue,
                        "opponent": spec.opponent,
                        "date": spec.date,
                        "source_video": video.name,
                        "timestamp_sec": round(t_sec, 2),
                        "orig_w": orig_w,
                        "orig_h": orig_h,
                        "stored_w": stored_w,
                        "stored_h": stored_h,
                        "keypoints_norm": kpts_norm,
                        "scores": scores,
                        "mean_score": round(mean_score, 4),
                        "gate_pass": mean_score >= GATE_THRESHOLD,
                        "teacher_sha256": teacher_sha,
                        "created_at": time.time(),
                    }
                    json_path.parent.mkdir(parents=True, exist_ok=True)
                    json_path.write_text(json.dumps(label), encoding="utf-8")

                    if args.render_overlays:
                        ov = _draw_overlay(cv2.imread(str(jpg_path)), kpts_norm, scores)
                        overlays_dir.mkdir(parents=True, exist_ok=True)
                        cv2.imwrite(str(overlays_dir / f"{stem}.png"), ov)

                    written += 1
                    score_sum += mean_score
                    remaining_for_group -= 1
                except Exception as e:  # one bad frame must not kill the game
                    logger.warning("Frame %s failed: %s", stem, e)
                    failed += 1
        except Exception as e:  # one bad/corrupt video must not kill the game
            logger.warning("Video %s failed: %s", video, e)
            failed += 1

    mean = score_sum / written if written else 0.0
    return {
        "game_id": spec.game_id,
        "team": spec.team,
        "venue": spec.venue,
        "videos": [v.name for v in spec.videos],
        "written": written,
        "skipped": skipped,
        "failed": failed,
        "mean_score": round(mean, 4),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate field keypoint labels")
    parser.add_argument(
        "--roots",
        type=Path,
        nargs="+",
        required=True,
        help="Directories whose subdirs are recording groups (Reolink footage)",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--teacher-model",
        type=Path,
        default=None,
        help="Path to the teacher keypoint ONNX (required unless --dry-run)",
    )
    parser.add_argument("--interval-sec", type=float, default=60.0)
    parser.add_argument("--max-per-group", type=int, default=150)
    parser.add_argument("--max-per-segment", type=int, default=8)
    parser.add_argument(
        "--groups", type=str, default=None, help="glob over group names"
    )
    parser.add_argument(
        "--use-segments",
        action="store_true",
        help="Sample raw segments even when combined.mp4 exists",
    )
    parser.add_argument("--render-overlays", action="store_true")
    parser.add_argument("--allow-unknown", action="store_true")
    parser.add_argument("--force", action="store_true", help="Regenerate existing")
    parser.add_argument("--cpu", action="store_true", help="Force CPU inference")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print routing table and exit without decoding",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    games = discover_games(args.roots, args.groups, args.use_segments)
    if not games:
        logger.error("No recording groups found under %s", args.roots)
        return
    bad_team = print_routing_table(games)

    if args.dry_run:
        return
    if bad_team and not args.allow_unknown:
        logger.error(
            "%d game(s) have match_info but an unrecognized team; fix "
            "my_team_name or pass --allow-unknown to skip them",
            bad_team,
        )
        return
    if args.teacher_model is None:
        parser.error("--teacher-model is required unless --dry-run")

    teacher_sha = _sha256(args.teacher_model)
    from video_grouper.inference.field_detector import create_field_session

    sess = create_field_session(args.teacher_model, use_gpu=not args.cpu)

    args.output_root.mkdir(parents=True, exist_ok=True)
    to_process = [g for g in games if g.team is not None]
    results = []
    for i, spec in enumerate(to_process, 1):
        logger.info("[%d/%d] %s (%s)", i, len(to_process), spec.game_id, spec.venue)
        results.append(process_game(spec, sess, args, teacher_sha))

    index_path = args.output_root / "dataset_index.json"
    index = {
        "schema_version": LABEL_SCHEMA_VERSION,
        "num_keypoints": NUM_KEYPOINTS,
        "teacher_sha256": teacher_sha,
        "updated_at": time.time(),
        "games": results,
        "totals": {
            "games": len(results),
            "frames": sum(r["written"] for r in results),
            "skipped": sum(r["skipped"] for r in results),
            "failed": sum(r["failed"] for r in results),
        },
    }
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    print("\n=== label generation summary ===")
    for r in sorted(results, key=lambda r: (r["team"], r["venue"])):
        print(
            f"  {r['team']:5} {r['venue'][:26]:26} "
            f"frames={r['written']:4} skip={r['skipped']:4} "
            f"fail={r['failed']:3} mean_score={r['mean_score']:.3f}"
        )
    t = index["totals"]
    print(
        f"\ntotal: {t['frames']} frames over {t['games']} games "
        f"(skipped={t['skipped']}, failed={t['failed']})"
    )
    print(f"index: {index_path}")


if __name__ == "__main__":
    main()
