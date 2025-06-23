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
    
    BASE_URL = "https://app.playmetrics.com"
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
            chrome_options.add_argument("--headless")  # Run in headless mode
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
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
                EC.presence_of_element_located((By.ID, "email"))
            )
            
            # Enter credentials
            self.driver.find_element(By.ID, "email").send_keys(self.username)
            self.driver.find_element(By.ID, "password").send_keys(self.password)
            
            # Click the login button
            self.driver.find_element(By.XPATH, "//button[@type='submit']").click()
            
            # Wait for the dashboard to load
            WebDriverWait(self.driver, 10).until(
                EC.url_contains("/dashboard")
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
                self.driver.get(self.DASHBOARD_URL)
                
                # Wait for the teams to load
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".team-card"))
                )
                
                # Click on the first team card
                self.driver.find_element(By.CSS_SELECTOR, ".team-card").click()
            else:
                # Go directly to the team page if we have the team ID
                self.driver.get(f"{self.BASE_URL}/teams/{self.team_id}")
            
            # Wait for the team page to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//a[contains(text(), 'Schedule')]"))
            )
            
            # Click on the Schedule tab
            self.driver.find_element(By.XPATH, "//a[contains(text(), 'Schedule')]").click()
            
            # Wait for the schedule to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".schedule-event"))
            )
            
            # Get the page source and parse it with BeautifulSoup
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            
            # Find all schedule events
            event_elements = soup.select(".schedule-event")
            
            # Calculate the date range
            today = datetime.now().date()
            end_date = today + timedelta(days=days_range)
            
            # Extract event information
            for event in event_elements:
                try:
                    # Extract date and check if it's within range
                    date_element = event.select_one(".event-date")
                    if not date_element:
                        continue
                        
                    # Parse the date (format may vary, adjust as needed)
                    date_str = date_element.text.strip()
                    event_date = datetime.strptime(date_str, "%m/%d/%Y").date()
                    
                    if not (today <= event_date <= end_date):
                        continue
                    
                    # Extract other event details
                    time_element = event.select_one(".event-time")
                    event_time = time_element.text.strip() if time_element else "TBD"
                    
                    title_element = event.select_one(".event-title")
                    event_title = title_element.text.strip() if title_element else "Unknown Event"
                    
                    location_element = event.select_one(".event-location")
                    event_location = location_element.text.strip() if location_element else "TBD"
                    
                    # Determine if this is a game or practice
                    is_game = "game" in event_title.lower() or "match" in event_title.lower()
                    
                    # Extract opponent name if it's a game
                    opponent_name = "Unknown"
                    if is_game and "vs" in event_title:
                        opponent_name = event_title.split("vs")[1].strip()
                    
                    # Create event dictionary
                    event_dict = {
                        "date": event_date.strftime("%Y-%m-%d"),
                        "time": event_time,
                        "title": event_title,
                        "location": event_location,
                        "is_game": is_game,
                        "opponent": opponent_name if is_game else None
                    }
                    
                    events.append(event_dict)
                    
                except Exception as e:
                    logger.error(f"Error parsing event: {str(e)}")
                    continue
            
            logger.info(f"Found {len(events)} upcoming events")
            return events
            
        except Exception as e:
            logger.error(f"Failed to get team events: {str(e)}")
            return []
    
    def find_game_for_recording(self, recording_date: datetime) -> Optional[Dict[str, Any]]:
        """
        Find a game that matches the recording date.
        
        Args:
            recording_date: The date of the recording
            
        Returns:
            Game information dictionary or None if no match found
        """
        if not self.enabled:
            return None
            
        # Get events with a wider range to ensure we catch games on the recording date
        events = self.get_team_events(days_range=14)
        
        # Convert recording_date to date object for comparison
        recording_day = recording_date.date()
        
        # Filter for games on the recording date
        matching_games = [
            event for event in events
            if event["is_game"] and datetime.strptime(event["date"], "%Y-%m-%d").date() == recording_day
        ]
        
        if not matching_games:
            logger.info(f"No games found for recording date {recording_day}")
            return None
        
        # If multiple games on the same day, return the first one
        if len(matching_games) > 1:
            logger.warning(f"Multiple games found for {recording_day}, using the first one")
        
        return matching_games[0]
    
    def populate_match_info(self, match_info, recording_date: datetime) -> bool:
        """
        Populate match info with data from PlayMetrics.
        
        Args:
            match_info: MatchInfo object to populate
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
            if hasattr(match_info, 'update_team_info'):
                # If match_info is the actual MatchInfo class instance
                match_info.update_team_info(team_info)
            else:
                # If match_info is a directory path string
                from video_grouper.models import MatchInfo
                MatchInfo.update_team_info(match_info, team_info)
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