"""
PlayMetrics API integration.

This module provides a way to get game information from PlayMetrics via their
REST API (primary) or calendar ICS file (fallback).
"""

import json
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
from video_grouper.utils.config import PlayMetricsConfig

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
                                "calendar_url": None,
                            }
                        )

                        logger.info(f"Extracted team from page title: {team_name}")
                except Exception as e:
                    logger.debug(f"Could not extract team from page title: {e}")

            # Method 4: Use the REST API to discover teams via roles + calendars
            if not teams:
                try:
                    teams = self._get_teams_via_api()
                    if teams:
                        logger.info(f"Found {len(teams)} teams via REST API")
                except Exception as e:
                    logger.debug(f"REST API team discovery failed: {e}")

            return teams

        except Exception as e:
            logger.error(f"Error getting available teams: {e}")
            return []

    def _get_teams_via_api(self) -> List[TeamInfo]:
        """Discover teams using the PlayMetrics REST API.

        Logs in via Firebase, fetches roles (clubs), then for each role
        fetches calendar data which contains team objects.  Returns a flat
        list of ``{id, name, calendar_url}`` dicts with ``name`` formatted
        as ``"Club — Team"`` for disambiguation.
        """
        roles = self._get_user_roles()
        if not roles:
            return []

        teams: List[TeamInfo] = []
        seen_ids: set = set()

        for role in roles:
            role_name = role.get("name", "")

            # Switch to this role
            self.driver.execute_script(
                """
                var vuex = JSON.parse(localStorage.getItem('vuex') || '{}');
                if (!vuex.auth) vuex.auth = {};
                vuex.auth.currentRole = arguments[0];
                vuex.auth.previousRoleID = arguments[0].id;
                localStorage.setItem('vuex', JSON.stringify(vuex));
                """,
                role,
            )
            self.driver.get(f"{self.BASE_URL}/calendar")
            time.sleep(6)

            # Fetch calendar data to get team objects
            raw = self._fetch_calendar_raw()
            if not raw or not isinstance(raw, list):
                continue

            for cal in raw:
                if not isinstance(cal, dict):
                    continue
                team = cal.get("team") or {}
                team_id = str(team.get("id", ""))
                team_name = team.get("name", "")
                if not team_name or team_id in seen_ids:
                    continue
                seen_ids.add(team_id)
                display = f"{role_name} — {team_name}" if role_name else team_name
                teams.append({"id": team_id, "name": display, "calendar_url": None})

        return teams

    def _fetch_calendar_raw(self) -> Optional[list]:
        """Fetch raw calendar JSON from the page context (no parsing)."""
        try:
            js_code = """
var callback = arguments[arguments.length - 1];
var dbReq = indexedDB.open('firebaseLocalStorageDb');
dbReq.onerror = function() { callback(null); };
dbReq.onsuccess = function(event) {
    var db = event.target.result;
    var tx = db.transaction('firebaseLocalStorage', 'readonly');
    var getAll = tx.objectStore('firebaseLocalStorage').getAll();
    getAll.onsuccess = function() {
        var firebaseToken = null;
        for (var i = 0; i < getAll.result.length; i++) {
            var val = getAll.result[i].value;
            if (val && val.stsTokenManager && val.stsTokenManager.accessToken) {
                firebaseToken = val.stsTokenManager.accessToken;
                break;
            }
        }
        if (!firebaseToken) { callback(null); return; }
        var vuex = JSON.parse(localStorage.getItem('vuex') || '{}');
        var currentRoleId = (vuex.auth && vuex.auth.currentRole)
            ? vuex.auth.currentRole.id : '';
        fetch('https://api.playmetrics.com/firebase/user/login', {
            method: 'POST',
            headers: {'Firebase-Token': firebaseToken, 'Content-Type': 'application/json'},
            body: JSON.stringify({current_role_id: currentRoleId, client_type: 'desktop'})
        })
        .then(function(r) { return r.json(); })
        .then(function(loginData) {
            var accessKey = loginData.access_key || '';
            var now = new Date();
            var startDate = new Date(now.getFullYear(), now.getMonth() - 1, 1);
            var endDate = new Date(now.getFullYear(), now.getMonth() + 6, 0);
            var filter = JSON.stringify({
                start_date: startDate.toISOString().split('T')[0],
                end_date: endDate.toISOString().split('T')[0],
                limit: 100, offset: 0, only_my_events: true
            });
            var apiUrl = 'https://api.playmetrics.com/user/calendars'
                + '?populate=team'
                + '&calendar_filter=' + encodeURIComponent(filter);
            fetch(apiUrl, {
                headers: {'Firebase-Token': firebaseToken, 'pm-access-key': accessKey}
            })
            .then(function(r) { return r.text(); })
            .then(function(text) { callback(text.substring(0, 500000)); })
            .catch(function() { callback(null); });
        })
        .catch(function() { callback(null); });
    };
};
"""
            raw = self.driver.execute_async_script(js_code)
            if not raw:
                return None
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.debug(f"_fetch_calendar_raw failed: {e}")
            return None

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

    def _get_events_via_rest_api(self) -> Optional[List[GameInfo]]:
        """
        Get events via PlayMetrics REST API using the authenticated Selenium session.

        The PlayMetrics SPA uses Firebase auth + pm-access-key headers. The API
        scopes calendars to the current role (club). Users may have multiple roles
        across clubs, so we try each role that might contain our team.

        Returns:
            List of event dicts, or None if the API approach failed.
        """
        if not self.driver or not self.logged_in:
            return None

        try:
            # Navigate to dashboard to trigger Firebase auth flow
            logger.info("Navigating to dashboard to establish API auth...")
            self.driver.get(f"{self.BASE_URL}/dashboard")
            time.sleep(5)

            # Get the user's roles from firebase/user/login response
            roles = self._get_user_roles()
            if not roles:
                logger.warning("Could not get user roles from PlayMetrics")
                return None

            logger.info(
                f"PlayMetrics user has {len(roles)} roles: "
                f"{[r.get('name', '?') for r in roles]}"
            )

            # Try each role to find calendars with games
            for role in roles:
                role_id = role.get("id", "")
                role_name = role.get("name", "?")
                logger.info(f"Trying role: {role_id} ({role_name})")

                # Switch to this role by updating Vuex localStorage
                self.driver.execute_script(
                    """
                    var vuex = JSON.parse(localStorage.getItem('vuex') || '{}');
                    if (!vuex.auth) vuex.auth = {};
                    vuex.auth.currentRole = arguments[0];
                    vuex.auth.previousRoleID = arguments[0].id;
                    localStorage.setItem('vuex', JSON.stringify(vuex));
                    """,
                    role,
                )

                # Reload calendar page with the new role
                self.driver.get(f"{self.BASE_URL}/calendar")
                time.sleep(6)

                # Fetch calendar data via JS in the page context
                events = self._fetch_calendar_from_page()
                if events:
                    logger.info(f"Found {len(events)} events under role {role_name}")
                    return events

            logger.warning("No events found under any role")
            return []

        except Exception as e:
            logger.error(f"PlayMetrics REST API failed: {e}")
            return None

    def _get_user_roles(self) -> list:
        """Extract user roles from the firebase/user/login API response."""
        try:
            result = self.driver.execute_async_script("""
var callback = arguments[arguments.length - 1];
var dbReq = indexedDB.open('firebaseLocalStorageDb');
dbReq.onerror = function() { callback('[]'); };
dbReq.onsuccess = function(event) {
    var db = event.target.result;
    var tx = db.transaction('firebaseLocalStorage', 'readonly');
    var getAll = tx.objectStore('firebaseLocalStorage').getAll();
    getAll.onsuccess = function() {
        var firebaseToken = null;
        for (var i = 0; i < getAll.result.length; i++) {
            var val = getAll.result[i].value;
            if (val && val.stsTokenManager) {
                firebaseToken = val.stsTokenManager.accessToken;
                break;
            }
        }
        if (!firebaseToken) { callback('[]'); return; }

        fetch('https://api.playmetrics.com/firebase/user/login', {
            method: 'POST',
            headers: {'Firebase-Token': firebaseToken, 'Content-Type': 'application/json'},
            body: '{}'
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            callback(JSON.stringify(data.roles || []));
        })
        .catch(function() { callback('[]'); });
    };
};
""")
            return json.loads(result) if result else []
        except Exception as e:
            logger.error(f"Failed to get user roles: {e}")
            return []

    def _fetch_calendar_from_page(self) -> Optional[List[GameInfo]]:
        """
        Fetch calendar data using the authenticated page context.

        After navigating to the calendar page with the correct role set in
        Vuex, this extracts the Firebase token and pm-access-key, then calls
        the calendar API.
        """
        try:
            js_code = """
var callback = arguments[arguments.length - 1];

var dbReq = indexedDB.open('firebaseLocalStorageDb');
dbReq.onerror = function() { callback(JSON.stringify({error: 'IndexedDB open failed'})); };
dbReq.onsuccess = function(event) {
    var db = event.target.result;
    var tx = db.transaction('firebaseLocalStorage', 'readonly');
    var getAll = tx.objectStore('firebaseLocalStorage').getAll();
    getAll.onsuccess = function() {
        var firebaseToken = null;
        for (var i = 0; i < getAll.result.length; i++) {
            var val = getAll.result[i].value;
            if (val && val.stsTokenManager && val.stsTokenManager.accessToken) {
                firebaseToken = val.stsTokenManager.accessToken;
                break;
            }
        }
        if (!firebaseToken) {
            callback(JSON.stringify({error: 'No Firebase token'}));
            return;
        }

        // Get the current role from Vuex store
        var vuex = JSON.parse(localStorage.getItem('vuex') || '{}');
        var currentRoleId = (vuex.auth && vuex.auth.currentRole)
            ? vuex.auth.currentRole.id : '';

        // Login with the specific role to get role-scoped access_key
        fetch('https://api.playmetrics.com/firebase/user/login', {
            method: 'POST',
            headers: {'Firebase-Token': firebaseToken, 'Content-Type': 'application/json'},
            body: JSON.stringify({current_role_id: currentRoleId, client_type: 'desktop'})
        })
        .then(function(r) { return r.json(); })
        .then(function(loginData) {
            var accessKey = loginData.access_key || '';

            var now = new Date();
            var startDate = new Date(now.getFullYear(), now.getMonth() - 1, 1);
            var endDate = new Date(now.getFullYear(), now.getMonth() + 6, 0);
            var filter = JSON.stringify({
                start_date: startDate.toISOString().split('T')[0],
                end_date: endDate.toISOString().split('T')[0],
                limit: 100,
                offset: 0,
                only_my_events: true
            });

            var apiUrl = 'https://api.playmetrics.com/user/calendars'
                + '?populate=team,team:games,team:games:league'
                + '&calendar_filter=' + encodeURIComponent(filter);

            fetch(apiUrl, {
                headers: {
                    'Firebase-Token': firebaseToken,
                    'pm-access-key': accessKey
                }
            })
            .then(function(r) { return r.text(); })
            .then(function(text) { callback(text.substring(0, 500000)); })
            .catch(function(e) { callback(JSON.stringify({error: e.message})); });
        })
        .catch(function(e) { callback(JSON.stringify({error: e.message})); });
    };
};
"""
            raw_result = self.driver.execute_async_script(js_code)
            if not raw_result:
                return None

            data = json.loads(raw_result)
            if isinstance(data, dict) and "error" in data:
                logger.warning(f"Calendar fetch error: {data['error']}")
                return None

            events = self._parse_api_calendars(data)
            # Return None (not empty list) if no events, so we try the next role
            return events if events else None

        except Exception as e:
            logger.error(f"Calendar page fetch failed: {e}")
            return None

    def _parse_api_calendars(self, data) -> List[GameInfo]:
        """
        Parse the PlayMetrics REST API calendar response into GameInfo objects.

        The API returns a list of calendar objects, each containing teams with
        games and practices.
        """
        events = []
        local_tz = self._get_configured_timezone()

        if not isinstance(data, list):
            logger.warning(f"Unexpected API response type: {type(data)}")
            if isinstance(data, dict):
                # Might be a single calendar object
                data = [data]
            else:
                return []

        for calendar in data:
            if not isinstance(calendar, dict):
                continue

            team = calendar.get("team") or {}
            team_name = team.get("name", "")
            team_games = team.get("games") or []

            for game in team_games:
                try:
                    # API uses start_datetime/end_datetime (ISO UTC)
                    start_str = (
                        game.get("start_datetime")
                        or game.get("start_date")
                        or game.get("date")
                    )
                    end_str = game.get("end_datetime") or game.get("end_date")

                    if not start_str:
                        continue

                    start_time = self._parse_api_datetime(start_str, local_tz)
                    end_time = (
                        self._parse_api_datetime(end_str, local_tz)
                        if end_str
                        else start_time + timedelta(hours=2)
                    )

                    # opponent_team_name is the direct field
                    opponent = game.get("opponent_team_name", "Unknown Opponent")
                    is_home = game.get("is_home", False)

                    # Location from nested field object
                    field_obj = game.get("field") or {}
                    location = (
                        field_obj.get("display_name", "")
                        or field_obj.get("facility_name", "")
                        or game.get("field_name", "")
                    )

                    # Build title
                    game_team_name = game.get("team_name", "") or team_name
                    title = game.get("title", "")
                    if not title:
                        if is_home:
                            title = f"{game_team_name} vs {opponent}"
                        else:
                            title = f"{game_team_name} @ {opponent}"

                    league = game.get("league") or {}
                    description = league.get("name", "")
                    game_type = (game.get("extra") or {}).get("game_type", "")
                    if game_type and not description:
                        description = game_type

                    event = {
                        "id": str(game.get("id", hash(f"{start_time}-{title}"))),
                        "title": title,
                        "description": description,
                        "location": location,
                        "start_time": start_time,
                        "end_time": end_time,
                        "is_game": True,
                        "is_home": is_home,
                        "opponent": opponent,
                        "my_team_name": self.team_name or game_team_name,
                    }

                    # Add display time
                    chosen_dt = start_time
                    if chosen_dt.tzinfo is None:
                        chosen_dt = chosen_dt.replace(tzinfo=pytz.UTC)
                    event["time"] = chosen_dt.astimezone(local_tz).strftime("%H:%M")

                    events.append(event)
                except Exception as e:
                    logger.error(f"Error parsing API game: {e}")

            # Also parse practices as non-game events
            team_practices = team.get("practices") or []
            for practice in team_practices:
                try:
                    start_str = (
                        practice.get("start_datetime")
                        or practice.get("start_date")
                        or practice.get("date")
                    )
                    if not start_str:
                        continue

                    start_time = self._parse_api_datetime(start_str, local_tz)
                    end_str = practice.get("end_datetime") or practice.get("end_date")
                    end_time = (
                        self._parse_api_datetime(end_str, local_tz)
                        if end_str
                        else start_time + timedelta(hours=1.5)
                    )

                    field_obj = practice.get("field") or {}
                    location = (
                        field_obj.get("display_name", "")
                        or field_obj.get("facility_name", "")
                        or practice.get("field_name", "")
                    )
                    title = practice.get("title") or f"{team_name} Practice"

                    event = {
                        "id": str(practice.get("id", hash(f"{start_time}-{title}"))),
                        "title": title,
                        "description": "",
                        "location": location,
                        "start_time": start_time,
                        "end_time": end_time,
                        "is_game": False,
                        "opponent": None,
                        "my_team_name": self.team_name or team_name,
                    }
                    events.append(event)
                except Exception as e:
                    logger.error(f"Error parsing API practice: {e}")

        logger.info(
            f"PlayMetrics REST API: Found {len(events)} events "
            f"({sum(1 for e in events if e.get('is_game'))} games)"
        )
        return events

    def _parse_api_datetime(self, dt_str: str, local_tz) -> datetime:
        """Parse a datetime string from the PlayMetrics API."""
        if not dt_str:
            raise ValueError("Empty datetime string")

        # Try ISO format first
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ]:
            try:
                dt = datetime.strptime(dt_str, fmt)
                if fmt.endswith("Z"):
                    dt = dt.replace(tzinfo=pytz.UTC)
                elif dt.tzinfo is None:
                    # Assume local timezone if no tz info
                    dt = local_tz.localize(dt)
                return dt
            except ValueError:
                continue

        # Try dateutil as last resort
        try:
            from dateutil import parser as dateutil_parser

            return dateutil_parser.parse(dt_str)
        except Exception:
            raise ValueError(f"Cannot parse datetime: {dt_str}")

    def get_events(self) -> List[GameInfo]:
        """
        Get all events from the PlayMetrics calendar.

        Tries the REST API first, falls back to ICS calendar scraping.

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

        # Try REST API first (new approach)
        events = self._get_events_via_rest_api()
        if events is not None:
            logger.info(f"Got {len(events)} events via REST API")
            self.events_cache = events
            self.last_cache_update = datetime.now()
            return events

        # Fall back to ICS calendar scraping (legacy approach)
        logger.info("REST API failed, falling back to ICS calendar scraping")
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

        # Parse games into (game, start_utc, end_utc) tuples for shared selection
        candidates = []
        for game in games:
            game_start = game.get("start_time")
            game_end = game.get("end_time")

            if not game_start:
                continue

            # Convert to UTC if needed
            if game_start.tzinfo is None:
                game_start = game_start.replace(tzinfo=timezone.utc)
            else:
                game_start = game_start.astimezone(timezone.utc)

            if game_end:
                if game_end.tzinfo is None:
                    game_end = game_end.replace(tzinfo=timezone.utc)
                else:
                    game_end = game_end.astimezone(timezone.utc)
            else:
                # Default to 2 hours if no end time
                game_end = game_start + timedelta(hours=2)

            candidates.append((game, game_start, game_end))

        from video_grouper.utils.game_selection import select_best_game

        best = select_best_game(
            candidates,
            recording_start,
            recording_end,
            game_label_fn=lambda g: g.get("title", "Unknown"),
        )

        if best is None:
            self._log_same_day_games(games, recording_start)

        return best

    def _log_same_day_games(self, games: list, recording_start: datetime) -> None:
        """Log all games on the same calendar day as the recording for diagnostics."""
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
            logger.warning(f"Diagnostic game-day logging failed: {e}")

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
