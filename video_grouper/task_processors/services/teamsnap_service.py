"""
TeamSnap service for match information lookup.
"""

import logging
import configparser
import os
from typing import Dict, List, Optional, Any
from datetime import datetime

from video_grouper.api_integrations.teamsnap import TeamSnapAPI

logger = logging.getLogger(__name__)


class TeamSnapService:
    """
    Service for TeamSnap API integration.
    Handles multiple team configurations and game lookups.
    """
    
    def __init__(self, config: configparser.ConfigParser, storage_path: str):
        """
        Initialize TeamSnap service.
        
        Args:
            config: Configuration object
            storage_path: Path to storage directory
        """
        self.config = config
        self.storage_path = storage_path
        self.teamsnap_apis = []
        self.enabled = False
        
        self._initialize_apis()
    
    def _initialize_apis(self) -> None:
        """Initialize TeamSnap API instances for all configured teams."""
        # Check for team configurations (TEAMSNAP.TEAM.*)
        team_configs = [section for section in self.config.sections() 
                       if section.startswith('TEAMSNAP.TEAM.')]
        
        # If no team configs found, use legacy configuration
        if not team_configs:
            if (self.config.has_section('TEAMSNAP') and 
                self.config.getboolean('TEAMSNAP', 'enabled', fallback=False)):
                logger.info("Using legacy TeamSnap configuration")
                config_path = os.path.join(self.storage_path, "config.ini")
                api = TeamSnapAPI(config_path)
                if api.enabled:
                    self.teamsnap_apis.append(api)
                    self.enabled = True
            return
            
        # Process each enabled team configuration
        for section in team_configs:
            if self.config.getboolean(section, 'enabled', fallback=False):
                logger.info(f"Initializing TeamSnap team: {section}")
                api = self._create_team_api(section)
                if api and api.enabled:
                    self.teamsnap_apis.append(api)
                    self.enabled = True
        
        if self.enabled:
            logger.info(f"TeamSnap service enabled with {len(self.teamsnap_apis)} teams")
        else:
            logger.info("TeamSnap service disabled - no valid configurations")
    
    def _create_team_api(self, section: str) -> Optional[TeamSnapAPI]:
        """Create a TeamSnap API instance for a specific team configuration."""
        try:
            # Create temporary config for this team
            temp_config = configparser.ConfigParser()
            temp_config.add_section('TEAMSNAP')
            
            # Copy base TeamSnap settings
            if self.config.has_section('TEAMSNAP'):
                for key, value in self.config['TEAMSNAP'].items():
                    if key not in ['team_id', 'team_name']:
                        temp_config['TEAMSNAP'][key] = value
            
            # Copy team-specific settings
            temp_config['TEAMSNAP']['enabled'] = 'true'
            for key, value in self.config[section].items():
                if key in ['team_id', 'team_name']:
                    temp_config['TEAMSNAP'][key] = value
            
            # Save temporary config
            temp_config_path = os.path.join(self.storage_path, f"temp_teamsnap_{section}.ini")
            with open(temp_config_path, 'w') as f:
                temp_config.write(f)
            
            # Create API instance
            api = TeamSnapAPI(temp_config_path)
            
            # Clean up temp file
            try:
                os.remove(temp_config_path)
            except Exception:
                pass
                
            return api
            
        except Exception as e:
            logger.error(f"Error creating TeamSnap API for {section}: {e}")
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
            
        for api in self.teamsnap_apis:
            try:
                game = api.find_game_for_recording(recording_start, recording_end)
                if game:
                    # Add source and team info
                    game['source'] = 'TeamSnap'
                    game['team_name'] = api.my_team_name
                    logger.info(f"Found TeamSnap game for team {api.my_team_name}: "
                              f"{game.get('team_name', 'Unknown')} vs {game.get('opponent_name', 'Unknown')}")
                    return game
            except Exception as e:
                logger.error(f"Error finding game in TeamSnap: {e}")
                
        return None
    
    def populate_match_info(self, group_dir: str, recording_start: datetime, recording_end: datetime) -> bool:
        """
        Populate match_info.ini with TeamSnap data if a game is found.
        
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
            
            # Convert TeamSnap game to match info format
            team_info = {
                'my_team_name': game.get('team_name', ''),
                'opponent_team_name': game.get('opponent_name', ''),
                'location': game.get('location_name', '')
            }
            
            # Update match info file
            MatchInfo.update_team_info(group_dir, team_info)
            logger.info(f"Updated match_info.ini with TeamSnap data for {group_dir}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating match info with TeamSnap data: {e}")
            return False 