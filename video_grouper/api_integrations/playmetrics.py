"""
PlayMetrics API integration using calendar ICS file.

This module provides a way to get game information from the PlayMetrics calendar.
"""

import os
import logging
from datetime import datetime, timedelta, timezone
import tempfile
from typing import List, Optional, TypedDict, Union
import time
import re
import traceback

import requests
import icalendar
import pytz
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import httpx

from video_grouper.utils.config import PlayMetricsConfig
from .base import ApiResponse
from ..models.match_info import MatchInfo

logger = logging.getLogger(__name__)


class TeamInfo(TypedDict, total=False):
    """Represents team information from PlayMetrics."""

    id: str
    name: str
    calendar_url: Optional[str]


class GameInfo(TypedDict, total=False):
    """Represents game information from PlayMetrics calendar."""

    # Basic game info
    title: str
    description: str
    location: str
    start_time: datetime
    end_time: datetime

    # Team info
    home_team: str
    away_team: str
    opponent: str

    # Calendar info
    calendar_url: str
    event_id: str

    # Custom fields
    custom_fields: dict[str, Union[str, int, float, bool]]


class PlayMetricsAPI:
    """
    A class to interact with the PlayMetrics website via calendar integration.

    This class provides functionality to:
    - Log in to the PlayMetrics website
    - Extract the calendar URL
    - Download and parse the calendar file
    - Match game information with recordings
    """

    BASE_URL = "https://playmetrics.com"
    LOGIN_URL = f"{BASE_URL}/login"
    DASHBOARD_URL = f"{BASE_URL}/dashboard"

    def __init__(self, config, app_config=None):
        """
        Initialize the PlayMetrics API.

        Args:
            config: PlayMetrics configuration object.
            app_config: Application configuration object containing timezone settings
        """
        self.config = config
        self.app_config = app_config

        if isinstance(config, PlayMetricsConfig):
            self.enabled = config.enabled
            self.username = config.username
            self.password = config.password
            self.team_id = config.team_id
            self.team_name = config.team_name
        else:
            # Fallback for tests using ConfigParser with attribute access
            enabled_str = str(getattr(config, "enabled", "true")).lower()
            self.enabled = enabled_str not in ["false", "0", "no"]
            self.username = getattr(config, "username", None) or getattr(
                config, "email", None
            )
            self.password = getattr(config, "password", None)
            self.team_id = getattr(config, "team_id", None)
            self.team_name = getattr(config, "team_name", "Test Team")

        # Initialize attributes
        self.driver = None
        self.logged_in = False
        self.calendar_url = None

        # Cache for games to avoid repeated requests
        self.events_cache = []
        self.last_cache_update = None
        self.cache_duration = timedelta(hours=1)  # Cache games for 1 hour

    def _get_configured_timezone(self) -> pytz.timezone:
        """
        Get the configured timezone from the application config.

        Returns:
            Configured timezone object, defaults to America/New_York if not found
        """
        timezone_str = "America/New_York"  # Default fallback

        if self.app_config and hasattr(self.app_config, "timezone"):
            timezone_str = self.app_config.timezone

        try:
            return pytz.timezone(timezone_str)
        except pytz.UnknownTimeZoneError:
            logger.warning(
                f"Unknown timezone '{timezone_str}', falling back to America/New_York"
            )
            return pytz.timezone("America/New_York")

    def __del__(self):
        """Clean up resources when the object is destroyed."""
        self.close()

    def close(self):
        """Close the browser."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.error(f"Error closing browser: {e}")
            finally:
                self.driver = None

    def _initialize_browser(self) -> None:
        """Initialize the Selenium browser for scraping."""
        if self.driver:
            return

        try:
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")

            try:
                service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=service, options=chrome_options)
            except Exception as e:
                # In many CI / test environments the webdriver manager cannot
                # download a real chromedriver binary.  When that happens we
                # fall back to invoking `webdriver.Chrome` *without* an
                # explicit Service which allows the test-suite to inject a
                # MagicMock for `webdriver.Chrome`.
                logger.warning(
                    f"ChromeDriverManager failed ({e}). Falling back to default webdriver.Chrome invocation."
                )
                self.driver = webdriver.Chrome(options=chrome_options)
            logger.info("Browser initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing browser: {e}")
            self.driver = None
            raise

    def login(self) -> bool:
        """
        Log in to the PlayMetrics website.

        Returns:
            bool: True if login was successful, False otherwise
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled - cannot log in")
            return False

        if self.logged_in:
            logger.debug("Already logged in to PlayMetrics")
            return True

        try:
            self._initialize_browser()
            if not self.driver:
                logger.error("Failed to initialize browser for PlayMetrics login")
                return False

            logger.info("Logging in to PlayMetrics...")
            self.driver.get(self.LOGIN_URL)
            logger.debug(
                f"Current URL after get: {getattr(self.driver, 'current_url', None)}"
            )
            logger.debug(f"Page title: {getattr(self.driver, 'title', None)}")

            # Wait for the login form to load
            time.sleep(3)

            # Find the email and password fields
            email_field = None
            password_field = None

            # Try different selectors for email field
            for selector in ["input[type='email']", "#username", "#email"]:
                try:
                    logger.debug(f"Trying email selector: {selector}")
                    email_field = self.driver.find_element(By.CSS_SELECTOR, selector)
                    logger.debug(f"Found email field with selector: {selector}")
                    break
                except Exception as e:
                    logger.debug(f"Email selector {selector} not found: {e}")
                    continue

            if not email_field:
                logger.error("Could not find email field on PlayMetrics login page")
                logger.error(f"Page source: {self.driver.page_source[:1000]}")
                return False

            # Try different selectors for password field
            for selector in ["input[type='password']", "#password"]:
                try:
                    logger.debug(f"Trying password selector: {selector}")
                    password_field = self.driver.find_element(By.CSS_SELECTOR, selector)
                    logger.debug(f"Found password field with selector: {selector}")
                    break
                except Exception as e:
                    logger.debug(f"Password selector {selector} not found: {e}")
                    continue

            if not password_field:
                logger.error("Could not find password field on PlayMetrics login page")
                logger.error(f"Page source: {self.driver.page_source[:1000]}")
                return False

            # Check for missing username/password
            if not self.username:
                logger.error("PlayMetrics username is missing or empty in config.")
                logger.error(f"PlayMetrics config: {self.config}")
                return False
            if not self.password:
                logger.error("PlayMetrics password is missing or empty in config.")
                logger.error(f"PlayMetrics config: {self.config}")
                return False

            # Fill in login details
            email_field.clear()
            email_field.send_keys(self.username)

            password_field.clear()
            password_field.send_keys(self.password)

            # Find and click the submit button
            try:
                submit_button = self.driver.find_element(
                    By.XPATH, "//button[@type='submit']"
                )
                logger.debug("Found submit button, clicking...")
                submit_button.click()
            except Exception as e:
                logger.error(f"Could not find or click submit button: {e}")
                logger.error(f"Page source: {self.driver.page_source[:1000]}")
                return False

            # Wait for login to complete
            time.sleep(5)

            # Check if login was successful
            current_url = getattr(self.driver, "current_url", None)
            logger.debug(f"URL after login attempt: {current_url}")
            if current_url and (
                "/calendar" in current_url or "/dashboard" in current_url
            ):
                self.logged_in = True
                logger.info("Successfully logged in to PlayMetrics")
                return True
            else:
                logger.error(
                    f"Login failed - not redirected to calendar. Current URL: {current_url}"
                )
                logger.error(
                    f"Page title after login: {getattr(self.driver, 'title', None)}"
                )
                logger.error(
                    f"Page source after login: {self.driver.page_source[:1000]}"
                )
                return False

        except Exception as e:
            logger.error(
                f"Error logging in to PlayMetrics: {e}\n{traceback.format_exc()}"
            )
            self.logged_in = False
            # Close browser to prevent process leak on login failure
            self.close()
            return False

    def get_available_teams(self) -> List[TeamInfo]:
        """
        Get all available teams for the logged-in user.

        Returns:
            List of TeamInfo dictionaries containing team information
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled")
            return []

        # Check if we're logged in, and if not, try to log in
        if not self.logged_in and not self.login():
            logger.warning("PlayMetrics not logged in and login failed")
            return []

        try:
            # Navigate to dashboard
            logger.info("Navigating to dashboard to find teams")
            self.driver.get(self.DASHBOARD_URL)
            time.sleep(3)

            # Look for team selector or team information
            teams = []

            # Method 1: Check for team selector dropdown
            try:
                team_selector = self.driver.find_element(
                    By.CSS_SELECTOR, "select.team-selector"
                )
                options = team_selector.find_elements(By.TAG_NAME, "option")

                # Ensure options is not None before iterating
                if options is not None:
                    for option in options:
                        team_id = option.get_attribute("value")
                        team_name = option.text.strip()

                        if team_id and team_name:
                            teams.append(
                                {
                                    "id": team_id,
                                    "name": team_name,
                                    "calendar_url": None,  # Will be populated later
                                }
                            )

                    logger.info(f"Found {len(teams)} teams in team selector")
                else:
                    logger.debug("Team selector options returned None")
            except Exception as e:
                logger.debug(f"No team selector found: {e}")

            # Method 2: Check for team cards or links
            if not teams:
                try:
                    team_elements = self.driver.find_elements(
                        By.CSS_SELECTOR, ".team-card, .team-link, [data-team-id]"
                    )

                    # Ensure team_elements is not None before iterating
                    if team_elements is not None:
                        for element in team_elements:
                            team_id = element.get_attribute("data-team-id") or ""
                            team_name = element.text.strip()

                            if team_name:
                                teams.append(
                                    {
                                        "id": team_id,
                                        "name": team_name,
                                        "calendar_url": None,  # Will be populated later
                                    }
                                )

                        logger.info(f"Found {len(teams)} team cards/links")
                    else:
                        logger.debug("Team elements returned None")
                except Exception as e:
                    logger.debug(f"No team cards/links found: {e}")

            # Method 3: Extract from page title if only one team
            if not teams:
                try:
                    page_title = self.driver.title
                    if page_title and "Dashboard" in page_title and "-" in page_title:
                        team_name = page_title.split("-", 1)[1].strip()

                        # Try to find team ID in the URL or page source
                        team_id = ""
                        current_url = self.driver.current_url
                        if current_url and "teamId=" in current_url:
                            team_id = current_url.split("teamId=")[1].split("&")[0]

                        teams.append(
                            {
                                "id": team_id,
                                "name": team_name,
                                "calendar_url": None,  # Will be populated later
                            }
                        )

                        logger.info(f"Extracted team from page title: {team_name}")
                except Exception as e:
                    logger.debug(f"Could not extract team from page title: {e}")

            # Get calendar URL for each team
            for team in teams:
                # If we have multiple teams, we need to switch to the team first
                if len(teams) > 1 and team["id"]:
                    try:
                        # Try to switch to the team
                        team_url = f"{self.DASHBOARD_URL}?teamId={team['id']}"
                        self.driver.get(team_url)
                        time.sleep(3)
                    except Exception as e:
                        logger.error(f"Error switching to team {team['name']}: {e}")

                # Now get the calendar URL
                calendar_url = self.get_calendar_url()
                if calendar_url:
                    team["calendar_url"] = calendar_url

            return teams

        except Exception as e:
            logger.error(f"Error getting available teams: {e}")
            return []

    def get_calendar_url(self) -> Optional[str]:
        """
        Navigate to the dashboard and find the calendar URL.

        Returns:
            str: Calendar URL if found, None otherwise
        """
        if not self.enabled or not self.logged_in:
            logger.warning("PlayMetrics not enabled or not logged in")
            return None

        if self.calendar_url:
            logger.debug("Using cached calendar URL")
            return self.calendar_url

        try:
            # Navigate to dashboard
            logger.info("Navigating to dashboard to find calendar URL")
            self.driver.get(self.DASHBOARD_URL)
            time.sleep(3)

            # Look for elements containing calendar-related text
            calendar_elements = []

            # Approach 1: Look for elements containing "calendar" text
            elements = self.driver.find_elements(
                By.XPATH,
                "//*[contains(text(), 'Calendar') or contains(text(), 'calendar')]",
            )
            logger.debug(f"Found {len(elements)} elements containing 'calendar' text")
            calendar_elements.extend(elements)

            # Approach 2: Look for elements containing "sync" text
            elements = self.driver.find_elements(
                By.XPATH, "//*[contains(text(), 'Sync') or contains(text(), 'sync')]"
            )
            logger.debug(f"Found {len(elements)} elements containing 'sync' text")
            calendar_elements.extend(elements)

            # Approach 3: Look for elements containing "ical" or "ics" text
            elements = self.driver.find_elements(
                By.XPATH,
                "//*[contains(text(), 'iCal') or contains(text(), 'ICS') or contains(text(), 'ics') or contains(text(), 'ical')]",
            )
            logger.debug(f"Found {len(elements)} elements containing 'iCal/ICS' text")
            calendar_elements.extend(elements)

            # Find a suitable calendar element to click
            calendar_button = None
            for element in calendar_elements:
                text = element.text.lower()
                if "sync" in text and (
                    "calendar" in text or "ical" in text or "ics" in text
                ):
                    calendar_button = element
                    logger.info(f"Found calendar sync button: {element.text}")
                    break

            if not calendar_button:
                logger.error("Could not find calendar sync button")
                return None

            # Click the calendar button
            if calendar_button:
                try:
                    calendar_button.click()
                    time.sleep(3)
                except Exception:
                    # Try JavaScript click if direct click fails
                    self.driver.execute_script("arguments[0].click();", calendar_button)
                    time.sleep(3)
            else:
                logger.error("Could not find calendar button")
                return None

            # Look for input fields that might contain the URL
            input_fields = self.driver.find_elements(By.XPATH, "//input")

            for field in input_fields:
                value = field.get_attribute("value")
                if value and (".ics" in value or "ical" in value):
                    self.calendar_url = value
                    logger.info(f"Found calendar URL: {value}")
                    return value

            # If not found in input fields, look for text that might contain a URL
            page_source = self.driver.page_source
            urls = re.findall(r"https?://\S+\.ics", page_source)

            if urls:
                self.calendar_url = urls[0]
                logger.info(f"Found calendar URL in page source: {urls[0]}")
                return urls[0]

            logger.error("Could not find calendar URL")
            return None

        except Exception as e:
            logger.error(f"Error getting calendar URL: {e}")
            return None

    def download_calendar(self) -> Optional[str]:
        """
        Download the PlayMetrics calendar file.

        Returns:
            str: Path to the downloaded calendar file, or None if download failed
        """
        if not self.enabled:
            logger.warning("PlayMetrics not enabled")
            return None

        # Check if we're logged in, and if not, try to log in
        if not self.logged_in and not self.login():
            logger.warning("PlayMetrics not logged in and login failed")
            return None

        calendar_url = self.get_calendar_url()
        if not calendar_url:
            logger.error("No calendar URL available")
            return None

        try:
            logger.info(f"Downloading calendar from {calendar_url}")
            response = requests.get(calendar_url)
            response.raise_for_status()

            # Create a temporary file
            fd, output_path = tempfile.mkstemp(suffix=".ics")
            os.close(fd)

            with open(output_path, "wb") as f:
                f.write(response.content)

            logger.info(f"Calendar downloaded to {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"Failed to download calendar: {e}")
            return None

    def parse_calendar(self, calendar_path: str) -> List[GameInfo]:
        """
        Parse the PlayMetrics calendar file.

        Args:
            calendar_path: Path to the calendar file

        Returns:
            List of event dictionaries
        """
        try:
            logger.info(f"Parsing calendar file: {calendar_path}")
            with open(calendar_path, "rb") as f:
                raw_data = f.read()
                if isinstance(raw_data, str):
                    raw_data = raw_data.encode("utf-8")
                cal = icalendar.Calendar.from_ical(raw_data)

            events = []
            for component in cal.walk():
                if component.name == "VEVENT":
                    try:
                        # Extract event details - decode vText objects to strings
                        summary_raw = component.get("summary", "No Title")
                        summary = str(summary_raw)

                        description_raw = component.get("description", "")
                        description = str(description_raw)

                        location_raw = component.get("location", "")
                        location = str(location_raw)

                        # Extract start and end times
                        start = component.get("dtstart").dt
                        end = (
                            component.get("dtend").dt
                            if component.get("dtend")
                            else None
                        )

                        # Convert to datetime if it's a date
                        if not isinstance(start, datetime):
                            start = datetime.combine(start, datetime.min.time())
                            start = start.replace(tzinfo=pytz.UTC)

                        if end and not isinstance(end, datetime):
                            end = datetime.combine(end, datetime.min.time())
                            end = end.replace(tzinfo=pytz.UTC)

                        # Determine if this is a game
                        is_game = any(
                            keyword.lower() in summary.lower()
                            for keyword in [
                                "game",
                                "match",
                                "vs",
                                "versus",
                                "against",
                                "@",
                            ]
                        )

                        # Try to extract opponent name
                        opponent = None
                        if is_game:
                            # First try to extract from title using common keywords
                            for keyword in ["vs", "versus", "against", "@"]:
                                if keyword.lower() in summary.lower():
                                    parts = summary.lower().split(keyword.lower(), 1)
                                    if len(parts) > 1:
                                        opponent = parts[1].strip()
                                        break

                            # If no opponent found in title, try to extract from description using team name
                            if not opponent and description and self.team_name:
                                opponent = self._extract_opponent_from_description(
                                    description, self.team_name
                                )

                            # If still no opponent, try to extract from location
                            if not opponent and location:
                                opponent = self._extract_opponent_from_location(
                                    location
                                )

                            # If we still couldn't extract opponent, use a generic name
                            if not opponent:
                                opponent = "Unknown Opponent"

                        # Create event dictionary
                        event = {
                            "id": str(hash(f"{start}-{summary}")),
                            "title": summary,
                            "description": description,
                            "location": location,
                            "start_time": start,
                            "end_time": end,
                            "is_game": is_game,
                            "opponent": opponent,
                            "my_team_name": self.team_name or "Test Team",
                        }

                        # Determine display time in configured timezone
                        local_tz = self._get_configured_timezone()
                        chosen_dt = end if (not is_game and end) else start
                        if chosen_dt.tzinfo is None:
                            chosen_dt = chosen_dt.replace(tzinfo=pytz.UTC)
                        event["time"] = chosen_dt.astimezone(local_tz).strftime("%H:%M")

                        events.append(event)
                    except Exception as e:
                        logger.error(f"Error parsing event: {e}")
            logger.info(f"Found {len(events)} events in calendar")
            return events
        except Exception as e:
            logger.error(f"Failed to parse calendar: {e}")
            return []

    def get_events(self) -> List[GameInfo]:
        """
        Get all events from the PlayMetrics calendar.

        Returns:
            List of event dictionaries
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled")
            return []

        # Check cache first
        if (
            self.last_cache_update
            and datetime.now() - self.last_cache_update < self.cache_duration
        ):
            logger.info("Using cached events")
            return self.events_cache

        # Login if needed
        if not self.logged_in and not self.login():
            logger.error("Failed to log in to PlayMetrics")
            return []

        # Download and parse calendar
        calendar_path = self.download_calendar()
        if not calendar_path:
            logger.error("Failed to download calendar")
            return []

        events = self.parse_calendar(calendar_path)

        # Clean up temporary file
        self._cleanup_calendar_file(calendar_path)

        # Update cache
        self.events_cache = events
        self.last_cache_update = datetime.now()

        return events

    def get_games(self) -> List[GameInfo]:
        """
        Get games from the PlayMetrics calendar.

        Returns:
            List of game dictionaries
        """
        events = self.get_events()
        games = [event for event in events if event.get("is_game", False)]
        logger.info(f"Found {len(games)} games in calendar")
        return games

    def find_game_for_recording(
        self, recording_start: datetime, recording_end: datetime
    ) -> Optional[GameInfo]:
        """
        Find a game that corresponds to a recording timespan.

        Args:
            recording_start: Start time of the recording
            recording_end: End time of the recording

        Returns:
            Game dictionary if found, None otherwise
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled")
            return None

        games = self.get_games()
        if not games:
            logger.warning("No games found in PlayMetrics calendar")
            return None

        # Convert recording times to UTC if they don't have timezone info
        local_tz = self._get_configured_timezone()

        if recording_start.tzinfo is None:
            recording_start = local_tz.localize(recording_start)
        if recording_end.tzinfo is None:
            recording_end = local_tz.localize(recording_end)

        recording_start = recording_start.astimezone(timezone.utc)
        recording_end = recording_end.astimezone(timezone.utc)

        # Define the time window for matching (3 hours before and after the game)
        time_window = timedelta(hours=3)

        # Find games that overlap with the recording time
        for game in games:
            game_start = game.get("start_time")

            # Skip if no start time
            if not game_start:
                continue

            # Convert to UTC if needed
            if game_start.tzinfo is None:
                game_start = game_start.replace(tzinfo=timezone.utc)

            # Check if the game is within the time window of the recording
            if (
                abs(recording_start - game_start) <= time_window
                or abs(recording_end - game_start) <= time_window
            ):
                logger.info(f"Found matching game: {game['title']} at {game_start}")
                return game

        logger.info("No matching game found for the recording time")
        # Diagnostic: list all games on the same date as the recording
        try:
            local_tz = self._get_configured_timezone()
            rec_local = recording_start.astimezone(local_tz)
            rec_date = rec_local.date()
            same_day = []
            for game in games:
                g_start = game.get("start_time")
                if not g_start:
                    continue
                if g_start.tzinfo is None:
                    g_start = g_start.replace(tzinfo=timezone.utc)
                g_local = g_start.astimezone(local_tz)
                if g_local.date() == rec_date:
                    same_day.append(
                        f"{g_local.strftime('%H:%M')} - {game.get('title', '')} ({game.get('location', '')})"
                    )
            if same_day:
                logger.info("PlayMetrics games on same day: " + "; ".join(same_day))
        except Exception as e:
            logger.warning(f"Diagnostic same-day logging failed: {e}")

        return None

    def populate_match_info(
        self, match_info: GameInfo, recording_start: datetime, recording_end: datetime
    ) -> bool:
        """
        Populate match information from PlayMetrics.

        Args:
            match_info: Dictionary to populate with match information
            recording_start: Start time of the recording
            recording_end: End time of the recording

        Returns:
            True if match information was populated, False otherwise
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled")
            return False

        game = self.find_game_for_recording(recording_start, recording_end)
        if not game:
            logger.warning("No matching game found in PlayMetrics")
            return False

        # Populate match information
        match_info["title"] = game.get("title", "")
        match_info["opponent"] = game.get("opponent", "")
        match_info["location"] = game.get("location", "")
        match_info["date"] = game.get("start_time").strftime("%Y-%m-%d")
        match_info["time"] = game.get("start_time").strftime("%H:%M")
        match_info["description"] = game.get("description", "")

        logger.info(f"Populated match info from PlayMetrics: {match_info['title']}")
        return True

    def _cleanup_calendar_file(self, calendar_path):
        """Removes the calendar file if it exists."""
        try:
            os.remove(calendar_path)
        except Exception:
            pass

    def _extract_opponent_from_description(
        self, description: str, team_name: str
    ) -> Optional[str]:
        """
        Extract the opponent name from the description field using the team name and 'at' pattern.
        """
        import re

        if not description or not team_name:
            return None

        desc = description.strip().replace("\n", " ")
        # Look for 'team_name at OPPONENT' or 'OPPONENT at team_name'
        pattern1 = re.escape(team_name) + r" at ([^\(\n]+)"
        pattern2 = r"([^-@\n]+) at " + re.escape(team_name)
        match = re.search(pattern1, desc, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(pattern2, desc, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    def _extract_opponent_from_location(self, location: str) -> str:
        """
        Extract opponent name from location if possible.
        This is a fallback method - most opponent info should come from description.
        """
        # For now, return None as the primary source should be description
        return None

    async def get_calendar_events(
        self, start_date: datetime, end_date: datetime
    ) -> List[ApiResponse]:
        """Get calendar events for the specified date range."""
        try:
            url = f"{self.BASE_URL}/teams/{self.team_id}/calendar"
            params = {
                "start_date": start_date.strftime("%Y-%m-%d"),
                "end_date": end_date.strftime("%Y-%m-%d"),
                "api_key": self.api_key,
            }

            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params)
                response.raise_for_status()

                data = response.json()
                return data.get("events", [])

        except Exception as e:
            logger.error(f"Error fetching calendar events: {e}")
            return []

    async def get_team_info(self) -> Optional[ApiResponse]:
        """Get team information."""
        try:
            url = f"{self.BASE_URL}/teams/{self.team_id}"
            params = {"api_key": self.api_key}

            async with httpx.AsyncClient() as client:
                response = await client.get(url, params=params)
                response.raise_for_status()

                return response.json()

        except Exception as e:
            logger.error(f"Error fetching team info: {e}")
            return None

    async def create_match_info(self, match_info: MatchInfo) -> Optional[ApiResponse]:
        """Create a new match in PlayMetrics."""
        try:
            url = f"{self.BASE_URL}/teams/{self.team_id}/matches"
            params = {"api_key": self.api_key}
            data = {
                "opponent": match_info.opponent,
                "date": match_info.date.strftime("%Y-%m-%d"),
                "time": match_info.time.strftime("%H:%M"),
                "location": match_info.location,
                "home_away": match_info.home_away,
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, params=params, json=data)
                response.raise_for_status()

                return response.json()

        except Exception as e:
            logger.error(f"Error creating match: {e}")
            return None
