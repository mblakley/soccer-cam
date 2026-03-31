"""Priority queue for human review of tracking gaps.

Serves the annotation app with the highest-value gaps first.
Gaps are scored by: Sonnet failure > gap length > track quality > displacement.

Human labels land in a pickup directory. The label merger collects them
at the start of each flywheel cycle.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

QUEUE_PATH = Path("D:/training_data/flywheel_human_queue.json")
HUMAN_LABELS_DIR = Path("D:/training_data/human_labels")


def load_queue() -> list[dict]:
    """Load the priority queue."""
    if not QUEUE_PATH.exists():
        return []
    with open(QUEUE_PATH) as f:
        return json.load(f)


def save_queue(queue: list[dict]):
    """Save the priority queue."""
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2)


def get_next(n: int = 1) -> list[dict]:
    """Get the next N highest-priority items for human review."""
    queue = load_queue()
    # Filter out already-reviewed items
    pending = [g for g in queue if not g.get("reviewed")]
    pending.sort(key=lambda g: g.get("priority", 0), reverse=True)
    return pending[:n]


def mark_reviewed(game_id: str, segment: str, frame_start: int, label: dict):
    """Mark a gap as reviewed by human, save the label.

    Args:
        game_id: Game identifier
        segment: Video segment name
        frame_start: Gap start frame
        label: Human-provided label: {"found": bool, "x": float, "y": float, "note": str}
    """
    # Save human label to pickup directory
    HUMAN_LABELS_DIR.mkdir(parents=True, exist_ok=True)
    label_file = HUMAN_LABELS_DIR / f"{game_id}_{segment}_{frame_start:06d}.json"
    with open(label_file, "w") as f:
        json.dump({
            "game_id": game_id,
            "segment": segment,
            "frame_start": frame_start,
            "label": label,
            "source": "human",
        }, f, indent=2)

    # Mark as reviewed in queue
    queue = load_queue()
    for g in queue:
        if g["game_id"] == game_id and g["segment"] == segment and g["frame_start"] == frame_start:
            g["reviewed"] = True
            g["review_result"] = label
            break
    save_queue(queue)

    logger.info("Human label saved: %s %s frame %d", game_id, segment, frame_start)


def queue_stats() -> dict:
    """Get queue statistics."""
    queue = load_queue()
    pending = [g for g in queue if not g.get("reviewed")]
    reviewed = [g for g in queue if g.get("reviewed")]
    found = [g for g in reviewed if g.get("review_result", {}).get("found")]

    return {
        "total": len(queue),
        "pending": len(pending),
        "reviewed": len(reviewed),
        "found": len(found),
        "top_priority": pending[0]["priority"] if pending else 0,
    }
