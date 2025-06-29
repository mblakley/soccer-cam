"""
PlayMetrics service for match information lookup.
"""

import logging
import configparser
import os
from typing import Dict, List, Optional, Any
from datetime import datetime

from video_grouper.api_integrations.playmetrics.api import PlayMetricsAPI

logger = logging.getLogger(__name__)


class PlayMetricsService:
    """
    Service for PlayMetrics API integration.
    Handles multiple team configurations and game lookups.
    """
    
    def __init__(self, config: configparser.ConfigParser, storage_path: str):
        """
        Initialize PlayMetrics service.
        
        Args:
            config: Configuration object
            storage_path: Path to storage directory
        """
        self.config = config
        self.storage_path = storage_path
        self.playmetrics_apis = []
        self.enabled = False
        
        self._initialize_apis()
    
    def _initialize_apis(self) -> None:
        """Initialize PlayMetrics API instances for all configured teams."""
        # Check for team configurations (PLAYMETRICS.*)
        team_configs = [section for section in self.config.sections() 
                       if section.startswith('PLAYMETRICS.') and section != 'PLAYMETRICS']
        
        # Process team-specific configurations first
        if team_configs:
            for section in team_configs:
                if self.config.getboolean(section, 'enabled', fallback=False):
                    logger.info(f"Initializing PlayMetrics team: {section}")
                    api = self._create_team_api(section)
                    if api and api.enabled and api.login():
                        self.playmetrics_apis.append(api)
                        self.enabled = True
        
        # Fallback to legacy configuration if no teams initialized
        if not self.playmetrics_apis and (self.config.has_section('PLAYMETRICS') and 
                                         self.config.getboolean('PLAYMETRICS', 'enabled', fallback=False)):
            logger.info("Using legacy PlayMetrics configuration")
            api = self._create_legacy_api()
            if api and api.enabled and api.login():
                self.playmetrics_apis.append(api)
                self.enabled = True
        
        if self.enabled:
            logger.info(f"PlayMetrics service enabled with {len(self.playmetrics_apis)} teams")
        else:
            logger.info("PlayMetrics service disabled - no valid configurations")
    
    def _create_team_api(self, section: str) -> Optional[PlayMetricsAPI]:
        """Create a PlayMetrics API instance for a specific team configuration."""
        try:
            # Create temporary config for this team
            temp_config = configparser.ConfigParser()
            temp_config.add_section('PLAYMETRICS')
            
            # Copy base PlayMetrics settings
            if self.config.has_section('PLAYMETRICS'):
                for key, value in self.config['PLAYMETRICS'].items():
                    if key not in ['team_id', 'team_name', 'username', 'password']:
                        temp_config['PLAYMETRICS'][key] = value
            
            # Copy team-specific settings
            temp_config['PLAYMETRICS']['enabled'] = 'true'
            for key, value in self.config[section].items():
                if key in ['team_id', 'team_name', 'username', 'password']:
                    temp_config['PLAYMETRICS'][key] = value
            
            # Save temporary config
            temp_config_path = os.path.join(self.storage_path, f"temp_playmetrics_{section}.ini")
            with open(temp_config_path, 'w') as f:
                temp_config.write(f)
            
            # Create API instance
            api = PlayMetricsAPI(temp_config_path)
            
            # Clean up temp file
            try:
                os.remove(temp_config_path)
            except Exception:
                pass
                
            return api
            
        except Exception as e:
            logger.error(f"Error creating PlayMetrics API for {section}: {e}")
            return None
    
    def _create_legacy_api(self) -> Optional[PlayMetricsAPI]:
        """Create a PlayMetrics API instance using legacy configuration."""
        try:
            # Create temporary config
            temp_config = configparser.ConfigParser()
            temp_config.add_section('PLAYMETRICS')
            
            # Copy all PlayMetrics settings
            for key, value in self.config['PLAYMETRICS'].items():
                temp_config['PLAYMETRICS'][key] = value
            
            # Save temporary config
            temp_config_path = os.path.join(self.storage_path, "temp_playmetrics_legacy.ini")
            with open(temp_config_path, 'w') as f:
                temp_config.write(f)
            
            # Create API instance
            api = PlayMetricsAPI(temp_config_path)
            
            # Clean up temp file
            try:
                os.remove(temp_config_path)
            except Exception:
                pass
                
            return api
            
        except Exception as e:
            logger.error(f"Error creating legacy PlayMetrics API: {e}")
            return None
    
    def find_game_for_recording(self, recording_start: datetime, recording_end: datetime) -> Optional[Dict[str, Any]]:
        """
        Find a game that matches the recording timespan.
        
        Args:
            recording_start: Start time of recording
            recording_end: End time of recording
            
        Returns:
            Game information dict or None if not found
        """
        if not self.enabled:
            return None
            
        for api in self.playmetrics_apis:
            try:
                game = api.find_game_for_recording(recording_start, recording_end)
                if game:
                    # Add source and team info
                    game['source'] = 'PlayMetrics'
                    game['team_name'] = api.team_name
                    logger.info(f"Found PlayMetrics game for team {api.team_name}: "
                              f"{game.get('title', 'Unknown')} vs {game.get('opponent', 'Unknown')}")
                    return game
            except Exception as e:
                logger.error(f"Error finding game in PlayMetrics: {e}")
                
        return None
    
    def populate_match_info(self, group_dir: str, recording_start: datetime, recording_end: datetime) -> bool:
        """
        Populate match_info.ini with PlayMetrics data if a game is found.
        
        Args:
            group_dir: Directory to create match_info.ini in
            recording_start: Start time of recording
            recording_end: End time of recording
            
        Returns:
            True if match info was populated, False otherwise
        """
        game = self.find_game_for_recording(recording_start, recording_end)
        if not game:
            return False
            
        try:
            from video_grouper.models import MatchInfo
            
            # Convert PlayMetrics game to match info format
            team_info = {
                'my_team_name': game.get('team_name', ''),
                'opponent_team_name': game.get('opponent', ''),
                'location': game.get('location', '')
            }
            
            # Add date/time if available
            if 'start_time' in game and game['start_time']:
                try:
                    start_time = game['start_time']
                    team_info['date'] = start_time.strftime('%Y-%m-%d')
                    team_info['time'] = start_time.strftime('%H:%M')
                except Exception:
                    pass
            
            # Update match info file
            MatchInfo.update_team_info(group_dir, team_info)
            logger.info(f"Updated match_info.ini with PlayMetrics data for {group_dir}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating match info with PlayMetrics data: {e}")
            return False
    
    def close(self) -> None:
        """Close all PlayMetrics API connections."""
        for api in self.playmetrics_apis:
            try:
                api.close()
            except Exception as e:
                logger.error(f"Error closing PlayMetrics API: {e}") 