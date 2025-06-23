"""
PlayMetrics web scraper for soccer-cam.

Since PlayMetrics doesn't provide a public API, we use web scraping to extract the necessary data.
"""

import os
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import configparser

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)

class PlayMetricsScraper:
    """
    A web scraper for PlayMetrics to extract game schedules and team information.
    
    This class uses Selenium to handle the login process and navigate through the PlayMetrics website
    to extract the necessary data for integration with soccer-cam.
    """
    
    BASE_URL = "https://playmetrics.com"
    LOGIN_URL = f"{BASE_URL}/login"
    DASHBOARD_URL = f"{BASE_URL}/dashboard"
    
    def __init__(self, config: configparser.ConfigParser):
        """
        Initialize the PlayMetrics scraper.
        
        Args:
            config: Configuration object containing PlayMetrics credentials
        """
        self.config = config
        self.enabled = config.getboolean("PLAYMETRICS", "enabled", fallback=False)
        self.username = config.get("PLAYMETRICS", "username", fallback="")
        self.password = config.get("PLAYMETRICS", "password", fallback="")
        self.team_id = config.get("PLAYMETRICS", "team_id", fallback="")
        self.headless = True  # Default to headless mode
        
        self.driver = None
        self.session = requests.Session()
        
        if not self.enabled:
            logger.info("PlayMetrics integration is disabled")
        elif not self.username or not self.password:
            logger.error("PlayMetrics credentials not configured")
            self.enabled = False
        else:
            logger.info("PlayMetrics integration is enabled")
    
    def initialize(self) -> bool:
        """
        Initialize the scraper by setting up the web driver.
        
        Returns:
            bool: True if initialization was successful, False otherwise
        """
        if not self.enabled:
            return False
            
        try:
            # Set up Chrome options
            chrome_options = Options()
            if self.headless:
                chrome_options.add_argument("--headless")  # Run in headless mode
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            if self.headless:
                chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            
            # Initialize the Chrome WebDriver
            self.driver = webdriver.Chrome(
                service=ChromeService(ChromeDriverManager().install()),
                options=chrome_options
            )
            
            logger.info("WebDriver initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {str(e)}")
            return False
    
    def login(self) -> bool:
        """
        Log in to the PlayMetrics website.
        
        Returns:
            bool: True if login was successful, False otherwise
        """
        if not self.enabled or not self.driver:
            return False
            
        try:
            logger.info("Logging in to PlayMetrics...")
            self.driver.get(self.LOGIN_URL)
            
            # Wait for the login form to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']"))
            )
            
            # Enter credentials
            email_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='email']")
            password_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            
            email_field.clear()
            email_field.send_keys(self.username)
            
            password_field.clear()
            password_field.send_keys(self.password)
            
            # Click the login button - try different selectors
            try:
                # First try to find by type
                login_button = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            except NoSuchElementException:
                try:
                    # Try to find by text content
                    login_button = self.driver.find_element(By.XPATH, "//button[contains(text(), 'Log In') or contains(text(), 'Sign In') or contains(text(), 'Login')]")
                except NoSuchElementException:
                    # Try to find any button in the form
                    login_button = self.driver.find_element(By.CSS_SELECTOR, "form button")
            
            login_button.click()
            
            # Wait for the dashboard to load - look for common dashboard elements
            WebDriverWait(self.driver, 15).until(
                EC.any_of(
                    EC.url_contains("/dashboard"),
                    EC.url_contains("/home"),
                    EC.url_contains("/teams"),
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".dashboard, .home, .teams, .user-profile"))
                )
            )
            
            logger.info("Successfully logged in to PlayMetrics")
            
            # Extract cookies from Selenium and add them to the requests session
            for cookie in self.driver.get_cookies():
                self.session.cookies.set(cookie['name'], cookie['value'])
            
            return True
        except TimeoutException:
            logger.error("Timed out waiting for login page or dashboard to load")
            return False
        except Exception as e:
            logger.error(f"Failed to log in to PlayMetrics: {str(e)}")
            return False
    
    def get_team_events(self, days_range: int = 7) -> List[Dict[str, Any]]:
        """
        Get upcoming team events from PlayMetrics.
        
        Args:
            days_range: Number of days to look ahead for events
            
        Returns:
            List of event dictionaries containing date, time, opponent, location, etc.
        """
        if not self.enabled or not self.driver:
            return []
            
        events = []
        
        try:
            # Navigate to the team schedule page
            if not self.team_id:
                logger.warning("Team ID not configured, trying to find team from dashboard")
                # First try to go to the dashboard
                self.driver.get(self.DASHBOARD_URL)
                
                # Wait for the page to load
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                
                # Try different selectors for team cards/links
                team_selectors = [
                    ".team-card", 
                    ".team-item", 
                    "a[href*='/teams/']",
                    "a[href*='/team/']",
                    ".team-link"
                ]
                
                team_element = None
                for selector in team_selectors:
                    try:
                        elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        if elements:
                            team_element = elements[0]
                            break
                    except:
                        continue
                
                if team_element:
                    team_element.click()
                else:
                    logger.error("Could not find any team links on the dashboard")
                    return []
            else:
                # Go directly to the team page if we have the team ID
                self.driver.get(f"{self.BASE_URL}/teams/{self.team_id}")
            
            # Wait for the team page to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            
            # Try different selectors for the Schedule tab/link
            schedule_selectors = [
                "//a[contains(text(), 'Schedule')]",
                "//a[contains(@href, 'schedule')]",
                "//a[contains(@href, 'events')]",
                "//a[contains(text(), 'Calendar')]",
                "//a[contains(text(), 'Events')]"
            ]
            
            schedule_element = None
            for selector in schedule_selectors:
                try:
                    elements = self.driver.find_elements(By.XPATH, selector)
                    if elements:
                        schedule_element = elements[0]
                        break
                except:
                    continue
            
            if schedule_element:
                schedule_element.click()
                
                # Wait for the schedule to load
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                
                # Give the page a moment to fully load the events
                time.sleep(2)
            else:
                logger.warning("Could not find Schedule tab, trying to find events on the current page")
            
            # Get the page source and parse it with BeautifulSoup
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            
            # Try different selectors for event elements
            event_selectors = [
                ".schedule-event",
                ".event-item",
                ".calendar-event",
                ".game-item",
                ".practice-item",
                ".event"
            ]
            
            event_elements = []
            for selector in event_selectors:
                elements = soup.select(selector)
                if elements:
                    event_elements = elements
                    logger.info(f"Found {len(elements)} events using selector '{selector}'")
                    break
            
            if not event_elements:
                logger.warning("Could not find any events on the page")
                return []
            
            # Calculate the date range
            today = datetime.now().date()
            end_date = today + timedelta(days=days_range)
            
            # Extract event information
            for event in event_elements:
                try:
                    # Try different selectors for date
                    date_element = None
                    for selector in [".event-date", ".date", "[data-date]", ".event-day"]:
                        date_element = event.select_one(selector)
                        if date_element:
                            break
                    
                    if not date_element:
                        continue
                    
                    # Extract date text
                    date_text = date_element.text.strip()
                    
                    # Try to parse the date
                    try:
                        # Try different date formats
                        date_formats = [
                            "%m/%d/%Y",  # 06/22/2025
                            "%Y-%m-%d",  # 2025-06-22
                            "%b %d, %Y", # Jun 22, 2025
                            "%B %d, %Y", # June 22, 2025
                            "%m/%d/%y",  # 06/22/25
                            "%d/%m/%Y",  # 22/06/2025
                            "%Y/%m/%d"   # 2025/06/22
                        ]
                        
                        event_date = None
                        for date_format in date_formats:
                            try:
                                event_date = datetime.strptime(date_text, date_format).date()
                                break
                            except ValueError:
                                continue
                        
                        if not event_date:
                            # Try to extract a date from a data attribute
                            date_attr = date_element.get("data-date")
                            if date_attr:
                                for date_format in date_formats:
                                    try:
                                        event_date = datetime.strptime(date_attr, date_format).date()
                                        break
                                    except ValueError:
                                        continue
                        
                        if not event_date:
                            logger.warning(f"Could not parse date: {date_text}")
                            continue
                        
                        # Skip events outside our date range
                        if event_date < today or event_date > end_date:
                            continue
                            
                        # Format the date consistently
                        formatted_date = event_date.strftime("%Y-%m-%d")
                    except Exception as e:
                        logger.warning(f"Error parsing date '{date_text}': {e}")
                        continue
                    
                    # Extract time
                    time_element = None
                    for selector in [".event-time", ".time", "[data-time]", ".event-hour"]:
                        time_element = event.select_one(selector)
                        if time_element:
                            break
                    
                    time_text = time_element.text.strip() if time_element else ""
                    
                    # Extract title/description
                    title_element = None
                    for selector in [".event-title", ".title", ".event-name", ".event-description", ".description"]:
                        title_element = event.select_one(selector)
                        if title_element:
                            break
                    
                    title_text = title_element.text.strip() if title_element else ""
                    
                    # Extract location
                    location_element = None
                    for selector in [".event-location", ".location", ".venue", ".address"]:
                        location_element = event.select_one(selector)
                        if location_element:
                            break
                    
                    location_text = location_element.text.strip() if location_element else ""
                    
                    # Determine if this is a game
                    is_game = False
                    opponent = None
                    
                    # Check if the title contains game-related keywords
                    game_keywords = ["game", "match", "vs", "versus", "against", "@"]
                    if any(keyword.lower() in title_text.lower() for keyword in game_keywords):
                        is_game = True
                        
                        # Try to extract opponent name
                        for keyword in ["vs", "versus", "against", "@"]:
                            if keyword.lower() in title_text.lower():
                                parts = title_text.lower().split(keyword.lower(), 1)
                                if len(parts) > 1:
                                    opponent = parts[1].strip()
                                    break
                                    
                        # If we couldn't extract opponent from the title, use the whole title
                        if not opponent and is_game:
                            opponent = title_text
                    
                    # Create the event dictionary
                    event_dict = {
                        "date": formatted_date,
                        "time": time_text,
                        "title": title_text,
                        "location": location_text,
                        "is_game": is_game,
                        "opponent": opponent
                    }
                    
                    events.append(event_dict)
                    
                except Exception as e:
                    logger.error(f"Error processing event: {e}")
                    continue
            
            logger.info(f"Found {len(events)} events in the date range")
            return events
            
        except Exception as e:
            logger.error(f"Failed to get team events: {str(e)}")
            return []
    
    def find_game_for_recording(self, recording_date: datetime) -> Optional[dict]:
        """
        Find a game that matches the recording date.
        
        Args:
            recording_date: The date of the recording
            
        Returns:
            dict: Game information or None if no match found
        """
        if not self.enabled:
            return None
            
        # Initialize the scraper if needed
        if not self.driver and not self.initialize():
            return None
            
        # Login if needed
        if not self.login():
            return None
            
        # Get team events
        events = self.get_team_events()
        if not events:
            logger.warning("No events found in PlayMetrics")
            return None
            
        # Filter for games only
        games = [event for event in events if event.get('is_game', False)]
        if not games:
            logger.warning("No games found in PlayMetrics events")
            return None
            
        # Find games that match the recording date
        matching_games = []
        for game in games:
            game_date_str = game.get('date')
            if not game_date_str:
                continue
                
            try:
                # Parse the game date
                game_date = datetime.strptime(game_date_str, '%Y-%m-%d').date()
                recording_date_only = recording_date.date()
                
                # Check if dates match
                if game_date == recording_date_only:
                    matching_games.append(game)
            except ValueError:
                logger.error(f"Failed to parse game date: {game_date_str}")
                continue
                
        # If no exact match found, try looking for games within 1 day
        if not matching_games:
            for game in games:
                game_date_str = game.get('date')
                if not game_date_str:
                    continue
                    
                try:
                    # Parse the game date
                    game_date = datetime.strptime(game_date_str, '%Y-%m-%d').date()
                    recording_date_only = recording_date.date()
                    
                    # Check if dates are within 1 day
                    delta = abs((game_date - recording_date_only).days)
                    if delta <= 1:
                        matching_games.append(game)
                except ValueError:
                    logger.error(f"Failed to parse game date: {game_date_str}")
                    continue
        
        # If still no match, return None
        if not matching_games:
            logger.warning(f"No matching games found in PlayMetrics for recording date {recording_date}")
            return None
            
        # If multiple matches found, sort by date proximity and return the closest
        if len(matching_games) > 1:
            matching_games.sort(key=lambda g: abs((datetime.strptime(g.get('date'), '%Y-%m-%d').date() - recording_date.date()).days))
            
        # Get team name from config
        team_name = self.config.get("PLAYMETRICS", "team_name", fallback="Our Team")
        
        # Add team name to the game info
        matching_games[0]['team_name'] = team_name
            
        return matching_games[0]
    
    def populate_match_info(self, group_dir, recording_date: datetime) -> bool:
        """
        Populate match info with data from PlayMetrics.
        
        Args:
            group_dir: Path to the group directory
            recording_date: The date of the recording
            
        Returns:
            bool: True if match info was populated, False otherwise
        """
        if not self.enabled:
            return False
            
        # Initialize the scraper if needed
        if not self.driver and not self.initialize():
            return False
            
        # Login if needed
        if not self.login():
            return False
            
        # Find a game that matches the recording date
        game = self.find_game_for_recording(recording_date)
        
        if not game:
            return False
        
        # Get team name from config or try to extract it
        team_name = self.config.get("PLAYMETRICS", "team_name", fallback="")
        if not team_name:
            try:
                # Navigate to team page to get the team name
                if self.team_id:
                    self.driver.get(f"{self.BASE_URL}/teams/{self.team_id}")
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".team-name"))
                    )
                    team_name_element = self.driver.find_element(By.CSS_SELECTOR, ".team-name")
                    team_name = team_name_element.text.strip()
                else:
                    team_name = "Our Team"  # Fallback
            except Exception as e:
                logger.error(f"Failed to get team name: {str(e)}")
                team_name = "Our Team"  # Fallback
        
        # Create team info dictionary
        team_info = {
            'team_name': team_name,
            'opponent_name': game["opponent"] or "Opponent",
            'location': game["location"]
        }
        
        # Update match info with the team info
        try:
            from video_grouper.models import MatchInfo
            MatchInfo.update_team_info(group_dir, team_info)
        except Exception as e:
            logger.error(f"Failed to update match info: {str(e)}")
            return False
        
        logger.info(f"Populated match info with PlayMetrics data: {team_name} vs {game['opponent']} at {game['location']}")
        return True
    
    def close(self):
        """Close the web driver and clean up resources."""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed successfully")
            except Exception as e:
                logger.error(f"Error closing WebDriver: {str(e)}")
            finally:
                self.driver = None 