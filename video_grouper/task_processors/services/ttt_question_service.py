"""TTT Pipeline Question Service — dual-path notification alongside NTFY.

Creates pipeline questions in TTT when NTFY notifications are sent.
Polls TTT API for responses. First response (NTFY or TTT) wins.
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class TTTQuestionService:
    """Manages pipeline questions created via the TTT API."""

    def __init__(self, ttt_api):
        self._ttt_api = ttt_api
        self._pending_questions: dict[str, str] = {}  # group_dir -> question_id

    async def create_question(
        self,
        team_id: str,
        question_type: str,
        title: str,
        message: str,
        actions: list[dict[str, str]] | None = None,
        recording_group_dir: str | None = None,
        image_url: str | None = None,
        camera_id: str | None = None,
    ) -> str | None:
        """Create a pipeline question in TTT. Returns question_id or None on failure."""
        try:
            result = self._ttt_api.create_pipeline_question(
                team_id=team_id,
                question_type=question_type,
                title=title,
                message=message,
                actions=actions,
                recording_group_dir=recording_group_dir,
                image_url=image_url,
                camera_id=camera_id,
            )
            question_id = result.get("id")
            if question_id and recording_group_dir:
                self._pending_questions[recording_group_dir] = question_id
            logger.info(
                "Created TTT pipeline question %s for %s",
                question_id,
                recording_group_dir,
            )
            return question_id
        except Exception as e:
            logger.warning("Failed to create TTT pipeline question: %s", e)
            return None

    async def poll_for_response(
        self,
        question_id: str,
        poll_interval: float = 5.0,
        timeout: float | None = None,
    ) -> str | None:
        """Poll TTT API for a response to a pipeline question.

        Returns the response_value or None if timed out / cancelled.
        """
        start = time.time()
        while True:
            try:
                result = self._ttt_api.get_pipeline_question(question_id)
                response = result.get("response_value")
                if response and response != "__cancelled__":
                    logger.info("TTT question %s answered: %s", question_id, response)
                    return response
            except Exception as e:
                logger.debug("TTT poll error for %s: %s", question_id, e)

            if timeout and (time.time() - start) > timeout:
                return None

            await asyncio.sleep(poll_interval)

    def cancel_question(self, recording_group_dir: str) -> None:
        """Cancel a pending question for a group dir (e.g., when NTFY responds first)."""
        question_id = self._pending_questions.pop(recording_group_dir, None)
        if question_id:
            try:
                self._ttt_api.cancel_pipeline_question(question_id)
                logger.info("Cancelled TTT question %s", question_id)
            except Exception as e:
                logger.debug("Failed to cancel TTT question %s: %s", question_id, e)

    def get_question_id(self, recording_group_dir: str) -> str | None:
        """Get the question ID for a pending question."""
        return self._pending_questions.get(recording_group_dir)
