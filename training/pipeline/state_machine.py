"""Game pipeline state machine — defines states and valid transitions.

Each game has exactly one pipeline state at any time. The orchestrator
advances states based on completed work items. Workers never touch state.

States:
    REGISTERED → STAGING → TILED → LABELING → LABELED →
    QA_PENDING → QA_DONE → REVIEW_PENDING → TRAINABLE

    EXCLUDED  — not trainable (futsal, indoor, gopro)
    FAILED:{stage} — failed at a stage, awaiting retry
    HOLD — manually paused by operator
"""

import logging

logger = logging.getLogger(__name__)


# All valid pipeline states
STATES = {
    "REGISTERED",
    "STAGING",
    "TILED",
    "LABELING",
    "LABELED",
    "QA_PENDING",
    "QA_DONE",
    "REVIEW_PENDING",
    "TRAINABLE",
    "EXCLUDED",
    "HOLD",
}

# Valid forward transitions: from_state → set of to_states
TRANSITIONS = {
    "REGISTERED": {"STAGING", "EXCLUDED", "HOLD"},
    "STAGING": {"TILED", "HOLD"},
    "TILED": {"LABELING", "LABELED", "HOLD"},
    "LABELING": {"LABELED", "HOLD"},
    "LABELED": {"QA_PENDING", "QA_DONE", "TRAINABLE", "HOLD"},
    "QA_PENDING": {"QA_DONE", "HOLD"},
    "QA_DONE": {"REVIEW_PENDING", "TRAINABLE", "HOLD"},
    "REVIEW_PENDING": {"TRAINABLE", "HOLD"},
    "TRAINABLE": {"HOLD"},  # can go back for re-training later
    "EXCLUDED": {"REGISTERED"},  # can un-exclude
    "HOLD": {  # can resume to any stage
        "REGISTERED",
        "STAGING",
        "TILED",
        "LABELING",
        "LABELED",
        "QA_PENDING",
        "QA_DONE",
        "REVIEW_PENDING",
        "TRAINABLE",
    },
}

# Which task type advances a game FROM each state
STATE_TO_TASK = {
    "REGISTERED": "stage",
    "STAGING": "tile",  # staging is done, now tile
    "TILED": "label",
    "LABELING": None,  # labeling in progress, wait for completion
    "LABELED": "sonnet_qa",
    "QA_PENDING": None,  # QA in progress
    "QA_DONE": "generate_review",  # auto-enqueue, but doesn't block training
    "REVIEW_PENDING": None,  # human review is async, doesn't block
}

# What state a game enters when a task type starts
TASK_START_STATE = {
    "stage": "STAGING",
    "tile": "STAGING",
    "label": "LABELING",
    "sonnet_qa": "QA_PENDING",
    "generate_review": "QA_DONE",  # stays in QA_DONE during review generation
}

# What state a game enters when a task type completes
TASK_COMPLETE_STATE = {
    "stage": "STAGING",  # staged but not yet tiled
    "tile": "TILED",
    "label": "LABELED",
    "sonnet_qa": "QA_DONE",
    "generate_review": "TRAINABLE",  # game is trainable immediately, human review is async
    "ingest_reviews": "TRAINABLE",  # human verdicts improve labels for next training run
}


def is_failed(state: str) -> bool:
    """Check if state represents a failure."""
    return state.startswith("FAILED:")


def get_failed_stage(state: str) -> str | None:
    """Extract the stage from a FAILED:stage state."""
    if is_failed(state):
        return state.split(":", 1)[1]
    return None


def can_transition(from_state: str, to_state: str) -> bool:
    """Check if a state transition is valid."""
    # FAILED states can go back to the failed stage OR advance forward
    if is_failed(from_state):
        failed_stage = get_failed_stage(from_state)
        if to_state == failed_stage or to_state == "HOLD":
            return True
        # Also allow advancing past the failed stage (task succeeded)
        allowed_from_failed = TRANSITIONS.get(failed_stage, set())
        return to_state in allowed_from_failed

    allowed = TRANSITIONS.get(from_state, set())
    # Also allow FAILED transitions from any active state
    if to_state.startswith("FAILED:"):
        return from_state not in {"EXCLUDED", "HOLD", "TRAINABLE"}

    return to_state in allowed


def next_task_for_game(state: str) -> str | None:
    """What task type should be enqueued for a game in this state?

    Returns None if no task is needed (game is waiting or terminal).
    """
    if is_failed(state):
        # Retry the failed stage
        return STATE_TO_TASK.get(get_failed_stage(state))
    return STATE_TO_TASK.get(state)


def advance_state(current: str, task_type: str, success: bool) -> str:
    """Determine the new state after a task completes or fails.

    Args:
        current: Current pipeline state
        task_type: The task that just completed/failed
        success: Whether the task succeeded

    Returns:
        New pipeline state
    """
    if not success:
        return f"FAILED:{current}"

    new_state = TASK_COMPLETE_STATE.get(task_type)
    if new_state:
        # Same state = no-op (stale task completion for already-advanced game)
        if new_state == current:
            return current
        if can_transition(current, new_state):
            return new_state

    # If no explicit transition, stay in current state.
    # Continuous QA on TRAINABLE/QA_DONE games is expected — don't warn.
    # Tasks that run on already-advanced games without changing state
    no_warn_tasks = {"sonnet_qa", "field_boundary"}
    if task_type in no_warn_tasks and current in ("TRAINABLE", "QA_DONE"):
        logger.debug(
            "%s on %s game — no state change (expected)",
            task_type,
            current,
        )
    else:
        logger.warning(
            "No state transition defined for %s completing %s (success=%s)",
            current,
            task_type,
            success,
        )
    return current


def infer_initial_state(
    *,
    has_video: bool = False,
    has_packs: bool = False,
    has_labels: bool = False,
    has_qa: bool = False,
    trainable: bool = True,
) -> str:
    """Infer initial pipeline state from existing data.

    Used during migration to set states for games that already have data.
    """
    if not trainable:
        return "EXCLUDED"
    if has_qa:
        return "TRAINABLE"
    if has_labels:
        return "LABELED"
    if has_packs:
        return "TILED"
    if has_video:
        return "REGISTERED"
    return "REGISTERED"
