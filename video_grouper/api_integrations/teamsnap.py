"""
TeamSnap API integration for video_grouper.
"""

import requests
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict, List, Optional, Any

from video_grouper.utils.config import TeamSnapConfig, TeamSnapTeamConfig

logger = logging.getLogger(__name__)


class TeamSnapAPI:
    """
    TeamSnap API integration for video_grouper.

    This class provides methods to interact with the TeamSnap API to fetch
    game information and populate match information.
    """

    def __init__(self, config: TeamSnapConfig, team_config: TeamSnapTeamConfig):
        """
        Initialize the TeamSnap API integration.

        Args:
            config: TeamSnap configuration object with OAuth credentials
            team_config: Team-specific configuration object
        """
        self.config = config
        self.team_config = team_config
        self.enabled = self.config.enabled and self.team_config.enabled
        self.access_token = self.config.access_token
        self.team_id = self.team_config.team_id
        self.team_name = self.team_config.team_name
        self.api_base_url = "https://api.teamsnap.com/v3"
        self.endpoints = {}

        if self.enabled:
            # Validate and refresh token if needed
            self._ensure_valid_token()
            # Discover API endpoints
            self._discover_api_endpoints()

    def _ensure_valid_token(self) -> bool:
        """
        Ensure we have a valid access token, refreshing if necessary.

        Returns:
            bool: True if we have a valid token, False otherwise
        """
        if not self.enabled:
            return False

        # If we have a token, test it first
        if self.access_token:
            if self._test_token():
                logger.debug("Existing TeamSnap access token is valid")
                return True
            else:
                logger.info(
                    "TeamSnap access token is expired or invalid, refreshing..."
                )

        # Get a new token
        return self.get_access_token()

    def _test_token(self) -> bool:
        """
        Test if the current access token is valid by making a simple API call.

        Returns:
            bool: True if token is valid, False otherwise
        """
        if not self.access_token:
            return False

        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
            }

            # Make a simple request to test the token
            response = requests.get(self.api_base_url, headers=headers, timeout=10)

            if response.status_code == 200:
                return True
            elif response.status_code == 401:
                logger.debug("TeamSnap token test returned 401 - token is invalid")
                return False
            else:
                logger.warning(f"TeamSnap token test returned {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Error testing TeamSnap token: {e}")
            return False

    def _make_api_request(
        self, url: str, method: str = "GET", params: Dict = None, json_data: Dict = None
    ) -> Optional[Dict]:
        """
        Make a request to the TeamSnap API.

        Args:
            url: API endpoint URL
            method: HTTP method (GET, POST, PATCH, DELETE)
            params: URL parameters
            json_data: JSON data for POST/PATCH requests

        Returns:
            Response JSON or None if request failed
        """
        if not self.enabled:
            logger.warning("TeamSnap API is not enabled")
            return None

        # Ensure we have a valid token before making the request
        if not self._ensure_valid_token():
            logger.error(
                "TeamSnap API is not enabled or cannot obtain valid access token"
            )
            return None

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }

        logger.debug(f"Making {method} request to {url}")

        try:
            if method == "GET":
                response = requests.get(url, headers=headers, params=params, timeout=30)
            elif method == "POST":
                response = requests.post(
                    url, headers=headers, params=params, json=json_data, timeout=30
                )
            elif method == "PATCH":
                response = requests.patch(
                    url, headers=headers, params=params, json=json_data, timeout=30
                )
            elif method == "DELETE":
                response = requests.delete(
                    url, headers=headers, params=params, timeout=30
                )
            else:
                logger.error(f"Unsupported method: {method}")
                return None

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                logger.warning(
                    "TeamSnap API returned 401 - token may be expired, attempting refresh"
                )
                # Try to refresh the token and retry the request once
                if self.get_access_token():
                    # Retry the request with the new token
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    if method == "GET":
                        response = requests.get(
                            url, headers=headers, params=params, timeout=30
                        )
                    elif method == "POST":
                        response = requests.post(
                            url,
                            headers=headers,
                            params=params,
                            json=json_data,
                            timeout=30,
                        )
                    elif method == "PATCH":
                        response = requests.patch(
                            url,
                            headers=headers,
                            params=params,
                            json=json_data,
                            timeout=30,
                        )
                    elif method == "DELETE":
                        response = requests.delete(
                            url, headers=headers, params=params, timeout=30
                        )

                    if response.status_code == 200:
                        return response.json()

                logger.error(
                    f"TeamSnap API request failed after token refresh: {response.status_code} - {response.text}"
                )
                return None
            else:
                logger.error(
                    f"TeamSnap API request failed: {response.status_code} - {response.text}"
                )
                return None
        except Exception as e:
            logger.error(f"TeamSnap API request exception: {e}")
            return None

    def _discover_api_endpoints(self) -> None:
        """
        Discover API endpoints by starting at the root endpoint.
        """
        logger.debug("Discovering TeamSnap API endpoints")

        # Start with the root endpoint
        root_response = self._make_api_request(self.api_base_url)

        if not root_response:
            logger.error("Failed to access the TeamSnap API root endpoint")
            return

        # Extract links from the root response
        if "collection" in root_response and "links" in root_response["collection"]:
            for link in root_response["collection"]["links"]:
                rel = link.get("rel")
                href = link.get("href")
                if rel and href:
                    self.endpoints[rel] = href
                    logger.debug(f"Discovered endpoint: {rel} -> {href}")

    def _find_link_by_rel(self, collection: Dict, rel: str) -> Optional[str]:
        """
        Find a link in a collection by its rel attribute.

        Args:
            collection: Collection+JSON response
            rel: The rel value to search for

        Returns:
            The href of the link, or None if not found
        """
        if "collection" in collection and "links" in collection["collection"]:
            for link in collection["collection"]["links"]:
                if link.get("rel") == rel:
                    return link.get("href")

        return None

    def _find_link_in_item(self, item: Dict, rel: str) -> Optional[str]:
        """
        Find a link in an item by its rel attribute.

        Args:
            item: Collection+JSON item
            rel: The rel value to search for

        Returns:
            The href of the link, or None if not found
        """
        if "links" in item:
            for link in item["links"]:
                if link.get("rel") == rel:
                    return link.get("href")

        return None

    def _extract_data_from_item(self, item: Dict) -> Dict[str, Any]:
        """
        Extract data fields from a Collection+JSON item.

        Args:
            item: Collection+JSON item

        Returns:
            Dictionary with data fields
        """
        result = {}

        if "data" in item:
            for data_field in item["data"]:
                name = data_field.get("name")
                value = data_field.get("value")
                if name is not None:  # Allow None values, just not None names
                    result[name] = value

        return result

    def get_team_events(self) -> List[Dict]:
        """
        Get events for the configured team.

        Returns:
            List of event dictionaries, or empty list if request failed
        """
        if not self.enabled or not self.team_id:
            logger.warning("TeamSnap API is not enabled or team ID is missing")
            return []

        # Check if we have the events endpoint
        if "events" not in self.endpoints:
            logger.error("Events endpoint not found")
            return []

        # Use the team_id to search for events
        events_url = f"{self.endpoints['events']}/search"
        params = {"team_id": self.team_id}

        logger.debug(f"Fetching team events from {events_url} with params {params}")

        events_data = self._make_api_request(events_url, params=params)

        if (
            not events_data
            or "collection" not in events_data
            or "items" not in events_data["collection"]
        ):
            logger.error("Failed to fetch team events")
            return []

        # Extract events from the response
        events = []
        for item in events_data["collection"]["items"]:
            event_data = self._extract_data_from_item(item)
            events.append(event_data)

        logger.info(f"Found {len(events)} team events")
        return events

    def get_games(self) -> List[Dict]:
        """
        Get games for the configured team.

        Returns:
            List of game dictionaries, or empty list if request failed
        """
        events = self.get_team_events()

        # Filter for games only
        games = [
            event
            for event in events
            if event.get("event_type") == "game" or event.get("is_game") is True
        ]

        logger.info(f"Found {len(games)} team games")
        return games

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> Optional[Dict]:
        """
        Find a game that corresponds to a recording timespan.

        Args:
            recording_start: Start time of the recording
            recording_end: End time of the recording

        Returns:
            Game dictionary if found, None otherwise
        """
        if not self.enabled:
            return None

        games = self.get_games()

        # Ensure recording times are timezone-aware
        if recording_start.tzinfo is None:
            recording_start = recording_start.replace(tzinfo=timezone.utc)
        if recording_end.tzinfo is None:
            recording_end = recording_end.replace(tzinfo=timezone.utc)

        # Look for games that overlap with the recording timespan
        for game in games:
            # Parse game start and end times
            game_start_str = game.get("start_date")
            if not game_start_str:
                continue

            try:
                # TeamSnap dates are in ISO format with Z for UTC
                game_start = datetime.fromisoformat(
                    game_start_str.replace("Z", "+00:00")
                )

                # Calculate game end time based on duration (default to 2 hours if not specified)
                duration_minutes = game.get("duration_in_minutes", 120)
                if isinstance(duration_minutes, str):
                    duration_minutes = int(duration_minutes)
                game_end = game_start + timedelta(minutes=duration_minutes)

                # Check if the recording overlaps with the game
                # (recording starts before game ends AND recording ends after game starts)
                if recording_start <= game_end and recording_end >= game_start:
                    logger.info(
                        f"Found matching game: {game.get('opponent_name')} at {game_start_str}"
                    )
                    return game
            except (ValueError, TypeError) as e:
                logger.error(f"Error parsing game date: {e}")

        logger.info("No matching game found for recording")
        return None

    def populate_match_info(
        self, match_info: Dict, recording_start: datetime, recording_end: datetime
    ) -> bool:
        """
        Populate match information based on TeamSnap game data.

        Args:
            match_info: Dictionary to populate with match information
            recording_start: Start time of the recording
            recording_end: End time of the recording

        Returns:
            True if match info was populated, False otherwise
        """
        if not self.enabled:
            return False

        game = self.find_game_for_recording(recording_start, recording_end)

        if not game:
            return False

        # Populate match info
        match_info["home_team"] = self.team_name
        match_info["away_team"] = game.get("opponent_name", "")
        match_info["location"] = game.get("location_name", "")

        # Parse the game date
        game_start_str = game.get("start_date")
        if game_start_str:
            try:
                game_start = datetime.fromisoformat(
                    game_start_str.replace("Z", "+00:00")
                )
                match_info["date"] = game_start.strftime("%Y-%m-%d")
                match_info["time"] = game_start.strftime("%H:%M")
            except (ValueError, TypeError) as e:
                logger.error(f"Error parsing game date: {e}")

        logger.info(f"Populated match info: {match_info}")
        return True

    def get_access_token(self) -> bool:
        """
        Get an OAuth access token using client credentials.

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.enabled:
            logger.warning("TeamSnap API is not enabled")
            return False

        client_id = self.config.client_id
        client_secret = self.config.client_secret

        if not client_id or not client_secret:
            logger.error("TeamSnap client ID or secret is missing")
            return False

        # OAuth token endpoint
        token_url = "https://auth.teamsnap.com/oauth/token"

        # Request body
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "read",
        }

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            logger.info("Requesting TeamSnap access token")
            response = requests.post(token_url, data=data, headers=headers, timeout=30)

            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data.get("access_token", "")

                # Store refresh token if provided (for future use)
                refresh_token = token_data.get("refresh_token")
                if refresh_token:
                    logger.debug("TeamSnap refresh token received")
                    # Note: We could store this in config for future refresh operations

                # Log token expiration if provided
                expires_in = token_data.get("expires_in")
                if expires_in:
                    logger.info(
                        f"TeamSnap access token expires in {expires_in} seconds"
                    )

                if self.access_token:
                    logger.info("Successfully obtained TeamSnap access token")
                    # Update the config with the new token
                    self._update_config_token()
                    # Discover API endpoints with the new token
                    self._discover_api_endpoints()
                    return True
                else:
                    logger.error("No access token in response")
                    return False
            else:
                logger.error(
                    f"TeamSnap token request failed: {response.status_code} - {response.text}"
                )
                return False

        except requests.exceptions.Timeout:
            logger.error("TeamSnap token request timed out")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"TeamSnap token request failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error getting TeamSnap access token: {e}")
            return False

    def _update_config_token(self) -> None:
        """
        Update the configuration with the new access token.
        This allows the token to persist across application restarts.
        """
        try:
            # Update the config object
            self.config.access_token = self.access_token

            # If the config has a method to save itself, call it
            if hasattr(self.config, "save"):
                self.config.save()
                logger.debug("TeamSnap access token saved to configuration")
            else:
                logger.debug(
                    "TeamSnap access token updated in memory (config save not available)"
                )

        except Exception as e:
            logger.warning(
                f"Could not save TeamSnap access token to configuration: {e}"
            )

    def test_connection(self) -> bool:
        """
        Test the TeamSnap connection and token validity.

        Returns:
            bool: True if connection is successful, False otherwise
        """
        if not self.enabled:
            logger.warning("TeamSnap API is not enabled")
            return False

        if not self._ensure_valid_token():
            logger.error("TeamSnap connection test failed - cannot obtain valid token")
            return False

        try:
            # Try to get teams as a connection test
            teams = self.get_teams()
            if teams is not None:
                logger.info(
                    f"TeamSnap connection test successful - found {len(teams)} teams"
                )
                return True
            else:
                logger.error("TeamSnap connection test failed - could not fetch teams")
                return False
        except Exception as e:
            logger.error(f"TeamSnap connection test failed with exception: {e}")
            return False

    def get_teams(self) -> List[Dict]:
        """
        Get all teams accessible to the user.

        Returns:
            List of team dictionaries, or empty list if request failed
        """
        if not self.enabled or not self.access_token:
            logger.warning("TeamSnap API is not enabled or access token is missing")
            return []

        # Check if we have the teams endpoint
        if "teams" not in self.endpoints:
            logger.error("Teams endpoint not found")
            return []

        teams_url = self.endpoints["teams"]

        logger.debug(f"Fetching teams from {teams_url}")

        teams_data = self._make_api_request(teams_url)

        if (
            not teams_data
            or "collection" not in teams_data
            or "items" not in teams_data["collection"]
        ):
            logger.error("Failed to fetch teams")
            return []

        # Extract teams from the response
        teams = []
        for item in teams_data["collection"]["items"]:
            team_data = self._extract_data_from_item(item)

            # Add team ID from href
            team_href = self._find_link_in_item(item, "self")
            if team_href:
                team_id = team_href.split("/")[-1]
                team_data["id"] = team_id

            # Only include active teams
            if team_data.get("is_active", False):
                teams.append(team_data)

        logger.info(f"Found {len(teams)} active teams")
        return teams
