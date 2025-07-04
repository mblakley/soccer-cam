"""
PlayMetrics API integration using calendar ICS file.

This module provides a way to get game information from the PlayMetrics calendar.
"""

import os
import logging
from datetime import datetime, timedelta, timezone
import tempfile
from typing import Dict, List, Optional
import time
import re

import requests
import icalendar
import pytz
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

from video_grouper.utils.config import PlayMetricsConfig

logger = logging.getLogger(__name__)


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

            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=chrome_options)
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
                return False

            logger.info("Logging in to PlayMetrics...")
            self.driver.get(self.LOGIN_URL)

            # Wait for the login form to load
            time.sleep(3)

            # Find the email and password fields
            email_field = None
            password_field = None

            # Try different selectors for email field
            for selector in ["input[type='email']", "#username", "#email"]:
                try:
                    email_field = self.driver.find_element(By.CSS_SELECTOR, selector)
                    break
                except Exception:
                    continue

            if not email_field:
                logger.error("Could not find email field")
                return False

            # Try different selectors for password field
            for selector in ["input[type='password']", "#password"]:
                try:
                    password_field = self.driver.find_element(By.CSS_SELECTOR, selector)
                    break
                except Exception:
                    continue

            if not password_field:
                logger.error("Could not find password field")
                return False

            # Fill in login details
            email_field.clear()
            email_field.send_keys(self.username)

            password_field.clear()
            password_field.send_keys(self.password)

            # Find and click the submit button
            submit_button = self.driver.find_element(
                By.XPATH, "//button[@type='submit']"
            )
            submit_button.click()

            # Wait for login to complete
            time.sleep(5)

            # Check if login was successful
            if "/dashboard" in self.driver.current_url:
                self.logged_in = True
                logger.info("Successfully logged in to PlayMetrics")
                return True
            else:
                logger.error("Login failed - not redirected to dashboard")
                return False

        except Exception as e:
            logger.error(f"Error logging in to PlayMetrics: {e}")
            self.logged_in = False
            return False

    def get_available_teams(self) -> List[Dict[str, str]]:
        """
        Get all available teams for the logged-in user.

        Returns:
            List of dictionaries containing team information:
            [
                {
                    'id': '12345',
                    'name': 'Team Name',
                    'calendar_url': 'https://...'
                },
                ...
            ]
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
            except Exception as e:
                logger.debug(f"No team selector found: {e}")

            # Method 2: Check for team cards or links
            if not teams:
                try:
                    team_elements = self.driver.find_elements(
                        By.CSS_SELECTOR, ".team-card, .team-link, [data-team-id]"
                    )

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
                except Exception as e:
                    logger.debug(f"No team cards/links found: {e}")

            # Method 3: Extract from page title if only one team
            if not teams:
                try:
                    page_title = self.driver.title
                    if "Dashboard" in page_title and "-" in page_title:
                        team_name = page_title.split("-", 1)[1].strip()

                        # Try to find team ID in the URL or page source
                        team_id = ""
                        if "teamId=" in self.driver.current_url:
                            team_id = self.driver.current_url.split("teamId=")[1].split(
                                "&"
                            )[0]

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

    def parse_calendar(self, calendar_path: str) -> List[Dict]:
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

    def get_events(self) -> List[Dict]:
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

    def get_games(self) -> List[Dict]:
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
            logger.warning("PlayMetrics integration not enabled")
            return None

        games = self.get_games()
        if not games:
            logger.warning("No games found in PlayMetrics calendar")
            return None

        # Convert recording times to UTC if they don't have timezone info
        local_tz = self._get_configured_timezone()

        if recording_start.tzinfo is None:
            recording_start = recording_start.replace(tzinfo=local_tz)
        if recording_end.tzinfo is None:
            recording_end = recording_end.replace(tzinfo=local_tz)

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
        self, match_info: Dict, recording_start: datetime, recording_end: datetime
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
