"""Pipeline tasks — atomic units of work executed by workers.

Each task follows the pull-local-process-push pattern:
1. Pull data from server share to local SSD
2. Process entirely on local SSD
3. Push results back to server share
4. Clean up local files

Task handlers are registered here and dispatched by the worker.
"""

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# Type for task handlers
TaskHandler = Callable[..., dict]

# Registry of task handlers — populated by imports below
_HANDLERS: dict[str, TaskHandler] = {}


def register_task(task_type: str):
    """Decorator to register a task handler."""

    def decorator(func: TaskHandler) -> TaskHandler:
        _HANDLERS[task_type] = func
        return func

    return decorator


def get_task_handler(task_type: str) -> TaskHandler | None:
    """Get the handler for a task type, importing lazily."""
    if not _HANDLERS:
        _load_handlers()
    return _HANDLERS.get(task_type)


def _load_handlers():
    """Import all task modules to populate the handler registry."""
    # Import each task module — their @register_task decorators
    # will populate _HANDLERS automatically
    try:
        from training.tasks import stage  # noqa: F401
    except ImportError:
        pass
    try:
        from training.tasks import tile  # noqa: F401
    except ImportError:
        pass
    try:
        from training.tasks import label  # noqa: F401
    except ImportError:
        pass
    try:
        from training.tasks import train  # noqa: F401
    except ImportError:
        pass
    try:
        from training.tasks import sonnet_qa  # noqa: F401
    except ImportError:
        pass
    try:
        from training.tasks import generate_review  # noqa: F401
    except ImportError:
        pass
    try:
        from training.tasks import ingest_reviews  # noqa: F401
    except ImportError:
        pass
    try:
        from training.tasks import field_boundary  # noqa: F401
    except ImportError:
        pass
    try:
        from training.tasks import phase_detect_task  # noqa: F401
    except ImportError:
        pass
    try:
        from training.tasks import build_shard  # noqa: F401
    except ImportError:
        pass


def list_tasks() -> list[str]:
    """Return all registered task types."""
    if not _HANDLERS:
        _load_handlers()
    return sorted(_HANDLERS.keys())
