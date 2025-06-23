"""
PlayMetrics API integration using web scraping.

This module provides a way to scrape game information from the PlayMetrics website.
"""

import os
import logging
import asyncio
import configparser
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import time

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)

class PlayMetricsAPI:
    """
    A class to interact with the PlayMetrics website via web scraping.
    
    This class provides functionality to:
    - Log in to the PlayMetrics website
    - Fetch upcoming games
    - Get game details
    - Match game information with recordings
    """
    
    BASE_URL = "https://app.playmetrics.io"
    LOGIN_URL = f"{BASE_URL}/login"
    DASHBOARD_URL = f"{BASE_URL}/dashboard"
    
    def __init__(self, config: configparser.ConfigParser):
        """
        Initialize the PlayMetrics API.
        
        Args:
            config: ConfigParser object containing PlayMetrics credentials
        """
        self.config = config
        self.enabled = config.getboolean("PLAYMETRICS", "enabled", fallback=False)
        
        if not self.enabled:
            logger.info("PlayMetrics integration not enabled")
            return
            
        self.username = config.get("PLAYMETRICS", "username", fallback=None)
        self.password = config.get("PLAYMETRICS", "password", fallback=None)
        self.team_id = config.get("PLAYMETRICS", "team_id", fallback=None)
        
        # Validate required configuration
        if not self.username or not self.password:
            logger.error("PlayMetrics username or password not configured")
            self.enabled = False
            return
            
        self.session = requests.Session()
        self.driver = None
        self.logged_in = False
        
        # Cache for games to avoid repeated requests
        self.games_cache = {}
        self.last_cache_update = None
        self.cache_duration = timedelta(hours=1)  # Cache games for 1 hour
    
    def __del__(self):
        """Clean up resources when the object is destroyed."""
        self.close()
    
    def close(self):
        """Close the session and browser."""
        if self.session:
            self.session.close()
            
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
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.ID, "email"))
            )
            
            # Fill in login details
            self.driver.find_element(By.ID, "email").send_keys(self.username)
            self.driver.find_element(By.ID, "password").send_keys(self.password)
            self.driver.find_element(By.XPATH, "//button[@type='submit']").click()
            
            # Wait for dashboard to load
            WebDriverWait(self.driver, 10).until(
                EC.url_contains("/dashboard")
            )
            
            self.logged_in = True
            logger.info("Successfully logged in to PlayMetrics")
            return True
            
        except Exception as e:
            logger.error(f"Error logging in to PlayMetrics: {e}")
            self.logged_in = False
            return False
    
    def get_games(self, days_ahead: int = 7) -> List[Dict[str, Any]]:
        """
        Get upcoming games from PlayMetrics.
        
        Args:
            days_ahead: Number of days ahead to look for games
            
        Returns:
            List of game dictionaries with details
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled - cannot get games")
            return []
            
        # Check cache first
        if self.last_cache_update and datetime.now() - self.last_cache_update < self.cache_duration:
            logger.info("Using cached games")
            return list(self.games_cache.values())
            
        if not self.login():
            logger.error("Failed to log in to PlayMetrics")
            return []
            
        games = []
        try:
            # Navigate to the team's schedule page
            if self.team_id:
                schedule_url = f"{self.BASE_URL}/teams/{self.team_id}/schedule"
            else:
                # If no team_id is specified, we'll try to find it from the dashboard
                schedule_url = self._find_team_schedule_url()
                
            if not schedule_url:
                logger.error("Could not find team schedule URL")
                return []
                
            logger.info(f"Navigating to schedule: {schedule_url}")
            self.driver.get(schedule_url)
            
            # Wait for the schedule to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".schedule-item"))
            )
            
            # Get the page source and parse with BeautifulSoup
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            
            # Find all schedule items
            schedule_items = soup.select(".schedule-item")
            
            # Calculate the date range we're interested in
            today = datetime.now().date()
            end_date = today + timedelta(days=days_ahead)
            
            for item in schedule_items:
                try:
                    # Extract date
                    date_elem = item.select_one(".schedule-date")
                    if not date_elem:
                        continue
                        
                    # Parse date (format may vary, this is an example)
                    date_text = date_elem.text.strip()
                    game_date = self._parse_date(date_text)
                    
                    # Skip if the game is outside our date range
                    if not game_date or game_date.date() < today or game_date.date() > end_date:
                        continue
                    
                    # Extract other game details
                    opponent = item.select_one(".opponent-name")
                    opponent_name = opponent.text.strip() if opponent else "Unknown"
                    
                    location = item.select_one(".location")
                    location_name = location.text.strip() if location else "Unknown"
                    
                    time_elem = item.select_one(".schedule-time")
                    time_text = time_elem.text.strip() if time_elem else ""
                    
                    # Extract game ID from the link
                    game_link = item.select_one("a")
                    game_id = None
                    if game_link and "href" in game_link.attrs:
                        href = game_link["href"]
                        # Extract the ID from the URL
                        if "/games/" in href:
                            game_id = href.split("/games/")[1].split("/")[0]
                    
                    # Create game dictionary
                    game = {
                        "id": game_id,
                        "date": game_date,
                        "opponent": opponent_name,
                        "location": location_name,
                        "time": time_text,
                        "url": f"{self.BASE_URL}/games/{game_id}" if game_id else None
                    }
                    
                    games.append(game)
                    
                    # Update cache
                    if game_id:
                        self.games_cache[game_id] = game
                    
                except Exception as e:
                    logger.error(f"Error parsing game: {e}")
            
            # Update cache timestamp
            self.last_cache_update = datetime.now()
            
            logger.info(f"Found {len(games)} upcoming games")
            return games
            
        except Exception as e:
            logger.error(f"Error getting games from PlayMetrics: {e}")
            return []
    
    def _find_team_schedule_url(self) -> Optional[str]:
        """
        Find the team schedule URL from the dashboard.
        
        Returns:
            The URL to the team's schedule page, or None if not found
        """
        try:
            self.driver.get(self.DASHBOARD_URL)
            
            # Wait for the dashboard to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".team-card"))
            )
            
            # Find team cards
            team_cards = self.driver.find_elements(By.CSS_SELECTOR, ".team-card")
            
            if not team_cards:
                logger.warning("No teams found on dashboard")
                return None
                
            # Use the first team if no specific team ID is configured
            team_card = team_cards[0]
            
            # Find the schedule link
            schedule_link = team_card.find_element(By.XPATH, ".//a[contains(@href, '/schedule')]")
            
            if not schedule_link:
                logger.warning("Schedule link not found")
                return None
                
            # Get the href attribute
            schedule_url = schedule_link.get_attribute("href")
            
            # Extract team ID from URL
            if "/teams/" in schedule_url:
                self.team_id = schedule_url.split("/teams/")[1].split("/")[0]
                logger.info(f"Found team ID: {self.team_id}")
            
            return schedule_url
            
        except Exception as e:
            logger.error(f"Error finding team schedule URL: {e}")
            return None
    
    def _parse_date(self, date_text: str) -> Optional[datetime]:
        """
        Parse a date string from PlayMetrics.
        
        Args:
            date_text: Date string to parse
            
        Returns:
            Parsed datetime object or None if parsing failed
        """
        date_formats = [
            "%A, %B %d, %Y",  # Monday, June 22, 2025
            "%b %d, %Y",      # Jun 22, 2025
            "%m/%d/%Y"        # 06/22/2025
        ]
        
        for date_format in date_formats:
            try:
                return datetime.strptime(date_text, date_format)
            except ValueError:
                continue
                
        logger.warning(f"Could not parse date: {date_text}")
        return None
    
    def get_game_details(self, game_id: str) -> Dict[str, Any]:
        """
        Get detailed information for a specific game.
        
        Args:
            game_id: ID of the game to get details for
            
        Returns:
            Dictionary with game details
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled - cannot get game details")
            return {}
            
        # Check if we have this game in the cache
        if game_id in self.games_cache:
            # If we already have detailed info, return it
            if "details_loaded" in self.games_cache[game_id]:
                return self.games_cache[game_id]
                
        if not self.login():
            logger.error("Failed to log in to PlayMetrics")
            return {}
            
        try:
            game_url = f"{self.BASE_URL}/games/{game_id}"
            logger.info(f"Getting details for game {game_id} from {game_url}")
            
            self.driver.get(game_url)
            
            # Wait for the game details to load
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".game-details"))
            )
            
            # Get the page source and parse with BeautifulSoup
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            
            # Extract game details
            details = {}
            
            # Basic info that should be in the cache already
            if game_id in self.games_cache:
                details = self.games_cache[game_id].copy()
            else:
                details["id"] = game_id
                
            # Extract additional details from the game page
            game_info = soup.select_one(".game-info")
            if game_info:
                # Extract team names
                teams = game_info.select(".team-name")
                if len(teams) >= 2:
                    details["team_name"] = teams[0].text.strip()
                    details["opponent_name"] = teams[1].text.strip()
                
                # Extract score if available
                scores = game_info.select(".score")
                if len(scores) >= 2:
                    details["team_score"] = scores[0].text.strip()
                    details["opponent_score"] = scores[1].text.strip()
            
            # Extract location details
            location_elem = soup.select_one(".location-details")
            if location_elem:
                details["location"] = location_elem.text.strip()
                
                # Try to extract address
                address_elem = soup.select_one(".location-address")
                if address_elem:
                    details["address"] = address_elem.text.strip()
            
            # Extract date and time
            datetime_elem = soup.select_one(".game-datetime")
            if datetime_elem:
                details["datetime"] = datetime_elem.text.strip()
            
            # Mark as having loaded details
            details["details_loaded"] = True
            
            # Update cache
            self.games_cache[game_id] = details
            
            return details
            
        except Exception as e:
            logger.error(f"Error getting game details from PlayMetrics: {e}")
            return {}
    
    def find_game_for_recording(self, recording_date: datetime, 
                               time_window_hours: int = 3) -> Optional[Dict[str, Any]]:
        """
        Find a game that matches the recording date.
        
        Args:
            recording_date: Date of the recording
            time_window_hours: Hours before/after to look for matching games
            
        Returns:
            Dictionary with game details or None if no match found
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled - cannot find game")
            return None
            
        # Get all games within a reasonable range
        days_ahead = 7
        days_behind = 1
        
        # Calculate days from today to recording date
        days_diff = (recording_date.date() - datetime.now().date()).days
        
        # Adjust days_ahead and days_behind based on the recording date
        if days_diff > 0:
            days_ahead = max(days_diff + 1, days_ahead)
            days_behind = 0
        elif days_diff < 0:
            days_ahead = 0
            days_behind = abs(days_diff) + 1
        
        # Get games
        games = self.get_games(days_ahead=days_ahead)
        
        # Filter games by date
        start_window = recording_date - timedelta(hours=time_window_hours)
        end_window = recording_date + timedelta(hours=time_window_hours)
        
        matching_games = []
        for game in games:
            game_date = game.get("date")
            if not game_date:
                continue
                
            # Check if the game is within the time window
            if start_window <= game_date <= end_window:
                matching_games.append(game)
        
        if not matching_games:
            logger.info(f"No matching games found for recording date {recording_date}")
            return None
            
        # If we have multiple matches, get the one closest to the recording time
        if len(matching_games) > 1:
            matching_games.sort(key=lambda g: abs((g.get("date") - recording_date).total_seconds()))
            
        # Get detailed info for the best match
        best_match = matching_games[0]
        game_id = best_match.get("id")
        
        if not game_id:
            logger.warning("Best matching game has no ID")
            return best_match
            
        # Get detailed info
        detailed_game = self.get_game_details(game_id)
        
        logger.info(f"Found matching game: {detailed_game.get('team_name', 'Unknown')} vs {detailed_game.get('opponent_name', 'Unknown')} at {detailed_game.get('location', 'Unknown')}")
        return detailed_game
    
    def populate_match_info(self, match_info, recording_date: datetime) -> bool:
        """
        Populate match info from PlayMetrics.
        
        Args:
            match_info: MatchInfo object to populate
            recording_date: Date of the recording
            
        Returns:
            True if match info was populated, False otherwise
        """
        if not self.enabled:
            logger.warning("PlayMetrics integration not enabled - cannot populate match info")
            return False
            
        # Find a matching game
        game = self.find_game_for_recording(recording_date)
        
        if not game:
            logger.warning(f"No matching game found for recording date {recording_date}")
            return False
            
        # Update match info
        team_info = {
            "team_name": game.get("team_name", ""),
            "opponent_name": game.get("opponent_name", ""),
            "location": game.get("location", "")
        }
        
        # Update the match info
        match_info.update_team_info(team_info)
        
        logger.info(f"Populated match info from PlayMetrics: {team_info}")
        return True 