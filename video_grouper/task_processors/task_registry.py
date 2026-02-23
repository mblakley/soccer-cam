"""
Task registry for managing task types and deserialization.
"""

import logging
from typing import Dict, Type, Optional

from .tasks.base_task import BaseTask
from .queue_type import QueueType
from video_grouper.models import RecordingFile

logger = logging.getLogger(__name__)


class TaskRegistry:
    """
    Registry for task types and their deserialization methods.
    """

    def __init__(self):
        self._task_types: Dict[str, Type[BaseTask]] = {}
        self._queue_task_types: Dict[QueueType, Dict[str, Type[BaseTask]]] = {}

    def register_task(self, task_class: Type[BaseTask]) -> None:
        """
        Register a task class for deserialization.

        Args:
            task_class: The task class to register
        """
        # Get task type and queue type from the class
        task_type = getattr(task_class, "task_type", None)
        queue_type = getattr(task_class, "queue_type", None)

        # Resolve property descriptors to their actual string values
        if isinstance(task_type, property):
            try:
                dummy = object.__new__(task_class)
                task_type = task_type.fget(dummy)
            except Exception:
                logger.error(
                    f"Task class {task_class.__name__}: could not resolve task_type property"
                )
                return

        # Enforce queue_type must be a class attribute or classmethod, not a property
        if isinstance(queue_type, property):
            logger.error(
                f"Task class {task_class.__name__} defines queue_type as a property. It must be a class attribute or @classmethod."
            )
            return
        if callable(queue_type):
            # If it's a classmethod or staticmethod, call it on the class
            queue_type = queue_type()

        if not task_type:
            logger.warning(
                f"Task class {task_class.__name__} has no task_type property"
            )
            return

        if not queue_type:
            logger.warning(
                f"Task class {task_class.__name__} has no queue_type property or classmethod"
            )
            return

        # Register by task type
        self._task_types[task_type] = task_class

        # Register by queue type
        if queue_type not in self._queue_task_types:
            self._queue_task_types[queue_type] = {}
        self._queue_task_types[queue_type][task_type] = task_class

        logger.debug(
            f"Registered task type '{task_type}' for queue '{queue_type.value}'"
        )

    def get_task_class(
        self, task_type: str, queue_type: Optional[QueueType] = None
    ) -> Optional[Type[BaseTask]]:
        """
        Get a task class by type.

        Args:
            task_type: The task type identifier
            queue_type: Optional queue type for additional validation

        Returns:
            The task class if found, None otherwise
        """
        if queue_type:
            # Look in specific queue type first
            if queue_type in self._queue_task_types:
                return self._queue_task_types[queue_type].get(task_type)

        # Fall back to general lookup
        return self._task_types.get(task_type)

    def deserialize_task(
        self, data: Dict[str, object], queue_type: Optional[QueueType] = None
    ) -> Optional[BaseTask]:
        """
        Deserialize a task from its data.

        Args:
            data: The serialized task data
            queue_type: Optional queue type for validation

        Returns:
            The deserialized task if successful, None otherwise
        """
        try:
            task_type = data.get("task_type")
            if not task_type:
                logger.warning(f"Skipping item without task_type: {data}")
                return None

            # Special handling for RecordingFile (used in download processor)
            if task_type == "recording_file":
                return RecordingFile.from_dict(data)

            task_class = self.get_task_class(task_type, queue_type)
            if not task_class:
                logger.error(
                    f"Unknown task type '{task_type}' for queue type {queue_type}"
                )
                return None

            return task_class.deserialize(data)

        except Exception as e:
            logger.error(f"Error deserializing task {data}: {e}")
            return None


# Global task registry instance
task_registry = TaskRegistry()
