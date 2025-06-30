"""
NTFY service for interactive user notifications and input.
"""

import json
import logging
import os
from typing import Dict, Optional, Any, Set
from datetime import datetime
import asyncio

from video_grouper.api_integrations.ntfy import NtfyAPI
from video_grouper.models import MatchInfo
from video_grouper.utils.config import NtfyConfig

logger = logging.getLogger(__name__)


class NtfyService:
    """
    Service for NTFY API integration with state tracking.
    Handles interactive notifications and tracks pending user inputs.
    """

    def __init__(self, config: NtfyConfig, storage_path: str):
        """
        Initialize NTFY service.

        Args:
            config: Configuration object
            storage_path: Path to storage directory
        """
        self.config = config
        self.storage_path = storage_path
        self.ntfy_api = None
        self.enabled = False

        # State tracking for pending user inputs
        self._pending_inputs: Dict[str, Dict[str, Any]] = {}
        self._processed_dirs: Set[str] = set()
        self._state_file = os.path.join(storage_path, "ntfy_service_state.json")

        # For handling direct responses to prompts
        self._response_events: Dict[str, asyncio.Event] = {}
        self._response_data: Dict[str, Optional[str]] = {}

        self._initialize_api()
        self._load_state()

    def _initialize_api(self) -> None:
        """Initialize NTFY API."""
        try:
            self.ntfy_api = NtfyAPI(self.config)
            self.enabled = self.ntfy_api.enabled

            if self.enabled:
                logger.info("NTFY service enabled")
            else:
                logger.info("NTFY service disabled")
        except Exception as e:
            logger.error(f"Error initializing NTFY API: {e}")
            self.enabled = False

    async def _ensure_initialized(self) -> bool:
        """Ensure NTFY API is fully initialized. Called before first use."""
        if not self.enabled or not self.ntfy_api:
            return False

        # Check if already initialized
        if hasattr(self.ntfy_api, "_initialized") and self.ntfy_api._initialized:
            return True

        try:
            await self.ntfy_api.initialize()
            # Mark as initialized to avoid repeated calls
            self.ntfy_api._initialized = True
            return True
        except Exception as e:
            logger.error(f"Error initializing NTFY API async components: {e}")
            self.enabled = False
            return False

    def _load_state(self) -> None:
        """Load service state from disk."""
        if not os.path.exists(self._state_file):
            return

        try:
            with open(self._state_file, "r") as f:
                state = json.load(f)

            self._pending_inputs = state.get("pending_inputs", {})
            self._processed_dirs = set(state.get("processed_dirs", []))

            logger.debug(
                f"NTFY service loaded state: {len(self._pending_inputs)} pending inputs, "
                f"{len(self._processed_dirs)} processed dirs"
            )

        except Exception as e:
            logger.error(f"Error loading NTFY service state: {e}")
            self._pending_inputs = {}
            self._processed_dirs = set()

    def _save_state(self) -> None:
        """Save service state to disk."""
        try:
            state = {
                "pending_inputs": self._pending_inputs,
                "processed_dirs": list(self._processed_dirs),
                "last_updated": datetime.now().isoformat(),
            }

            with open(self._state_file, "w") as f:
                json.dump(state, f, indent=2)

        except Exception as e:
            logger.error(f"Error saving NTFY service state: {e}")

    def is_waiting_for_input(self, group_dir: str) -> bool:
        """
        Check if we're waiting for user input for a specific directory.

        Args:
            group_dir: Directory path to check

        Returns:
            True if waiting for input, False otherwise
        """
        return group_dir in self._pending_inputs

    def has_been_processed(self, group_dir: str) -> bool:
        """
        Check if a directory has already been processed with NTFY.

        Args:
            group_dir: Directory path to check

        Returns:
            True if already processed, False otherwise
        """
        return group_dir in self._processed_dirs

    def mark_as_processed(self, group_dir: str) -> None:
        """
        Mark a directory as processed.

        Args:
            group_dir: Directory path to mark
        """
        self._processed_dirs.add(group_dir)
        # Remove from pending inputs if it was there
        self._pending_inputs.pop(group_dir, None)
        self._save_state()

    def mark_waiting_for_input(
        self, group_dir: str, input_type: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Mark a directory as waiting for user input.

        Args:
            group_dir: Directory path
            input_type: Type of input waiting for (e.g., 'team_info', 'game_times')
            metadata: Additional metadata about the pending input
        """
        self._pending_inputs[group_dir] = {
            "input_type": input_type,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        self._save_state()

    def clear_pending_input(self, group_dir: str) -> None:
        """
        Clear pending input status for a directory.

        Args:
            group_dir: Directory path
        """
        if group_dir in self._pending_inputs:
            del self._pending_inputs[group_dir]
            self._save_state()

    async def request_team_info(
        self,
        group_dir: str,
        combined_video_path: str,
        existing_info: Optional[Dict[str, str]] = None,
        force: bool = False,
    ) -> bool:
        """
        Request team information from user via NTFY.

        Args:
            group_dir: Directory path
            combined_video_path: Path to combined video
            existing_info: Existing team info if any
            force: Force request even if already processed

        Returns:
            True if request was sent, False otherwise
        """
        if not await self._ensure_initialized():
            return False

        # Check if already processed or waiting
        if not force:
            if self.has_been_processed(group_dir):
                logger.debug(f"Directory {group_dir} already processed with NTFY")
                return False

            if self.is_waiting_for_input(group_dir):
                logger.debug(f"Already waiting for input for {group_dir}")
                return False

        try:
            # Send team info request
            await self.ntfy_api.ask_team_info(combined_video_path, existing_info or {})

            # Mark as waiting for input
            self.mark_waiting_for_input(
                group_dir,
                "team_info",
                {
                    "combined_video_path": combined_video_path,
                    "existing_info": existing_info,
                },
            )

            logger.info(f"Sent team info request for {group_dir}")
            return True

        except Exception as e:
            logger.error(f"Error sending team info request for {group_dir}: {e}")
            return False

    async def request_game_times(
        self, group_dir: str, combined_video_path: str, force: bool = False
    ) -> bool:
        """
        Request game start/end times from user via NTFY.

        Args:
            group_dir: Directory path
            combined_video_path: Path to combined video
            force: Force request even if already processed

        Returns:
            True if request was sent, False otherwise
        """
        if not await self._ensure_initialized():
            return False

        # Check if already processed or waiting
        if not force:
            if self.has_been_processed(group_dir):
                logger.debug(f"Directory {group_dir} already processed with NTFY")
                return False

            if self.is_waiting_for_input(group_dir):
                logger.debug(f"Already waiting for input for {group_dir}")
                return False

        try:
            # Send game start time request
            await self.ntfy_api.ask_game_start_time(combined_video_path, group_dir)

            # Check if we should also ask for end time
            match_info, _ = MatchInfo.get_or_create(group_dir)
            if match_info and match_info.start_time_offset:
                await self.ntfy_api.ask_game_end_time(
                    combined_video_path, group_dir, match_info.start_time_offset
                )

            # Mark as waiting for input
            self.mark_waiting_for_input(
                group_dir, "game_times", {"combined_video_path": combined_video_path}
            )

            logger.info(f"Sent game times request for {group_dir}")
            return True

        except Exception as e:
            logger.error(f"Error sending game times request for {group_dir}: {e}")
            return False

    async def process_combined_directory(
        self, group_dir: str, combined_video_path: str, force: bool = False
    ) -> bool:
        """
        Process a combined directory with NTFY notifications.

        Args:
            group_dir: Directory path
            combined_video_path: Path to combined video
            force: Force processing even if already done

        Returns:
            True if processing was initiated, False otherwise
        """
        if not await self._ensure_initialized():
            return False

        # Check if match info is already populated
        match_info, _ = MatchInfo.get_or_create(group_dir)

        if not force and match_info and match_info.is_populated():
            logger.debug(f"Match info already populated for {group_dir}")
            self.mark_as_processed(group_dir)
            return False

        # Get existing team info
        existing_info = {}
        if match_info:
            existing_info = match_info.get_team_info()

        # Request team info first
        team_info_sent = await self.request_team_info(
            group_dir, combined_video_path, existing_info, force
        )

        # Request game times
        game_times_sent = await self.request_game_times(
            group_dir, combined_video_path, force
        )

        return team_info_sent or game_times_sent

    async def request_playlist_name(self, group_dir: str, team_name: str) -> bool:
        """
        Request the base YouTube playlist name from the user via NTFY.

        Args:
            group_dir: The directory associated with the request.
            team_name: The name of the team.

        Returns:
            True if the request was sent, False otherwise.
        """
        if not await self._ensure_initialized():
            return False

        if self.is_waiting_for_input(group_dir):
            logger.debug(f"Already waiting for playlist name for {group_dir}")
            return False

        try:
            question = f"Please provide the base YouTube playlist name for the team: '{team_name}'."
            await self.ntfy_api.send_notification(
                title="Playlist Name Needed",
                message=question,
                tags=["youtube_playlist_name", group_dir],
            )
            self.mark_waiting_for_input(
                group_dir, "playlist_name", {"team_name": team_name}
            )
            logger.info(f"Sent playlist name request for {group_dir}")
            return True
        except Exception as e:
            logger.error(f"Error sending playlist name request for {group_dir}: {e}")
            return False

    def get_pending_inputs(self) -> Dict[str, Dict[str, Any]]:
        """Get all pending inputs."""
        return self._pending_inputs.copy()

    def get_processed_directories(self) -> Set[str]:
        """Get all processed directories."""
        return self._processed_dirs.copy()

    async def shutdown(self) -> None:
        """Shutdown the NTFY service."""
        if self.ntfy_api:
            await self.ntfy_api.shutdown()
        self._save_state()
