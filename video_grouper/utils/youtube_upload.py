import os
import logging
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
import google.oauth2.credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from video_grouper.utils.locking import FileLock
from video_grouper.models import MatchInfo
from video_grouper.utils.directory_state import DirectoryState
import configparser

logger = logging.getLogger(__name__)

# If modifying these scopes, delete the token.json file.
SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/youtube',  # Required for playlist operations
]
API_SERVICE_NAME = 'youtube'
API_VERSION = 'v3'

# Default paths for YouTube credentials and token
YOUTUBE_DIR = "youtube"
CREDENTIALS_FILENAME = "client_secret.json"
TOKEN_FILENAME = "token.json"

def get_youtube_paths(storage_path: str) -> Tuple[str, str]:
    """Get the paths for YouTube credentials and token files.
    
    Args:
        storage_path: Base storage path from STORAGE.path
        
    Returns:
        Tuple[str, str]: (credentials_file_path, token_file_path)
    """
    youtube_dir = os.path.join(storage_path, YOUTUBE_DIR)
    os.makedirs(youtube_dir, exist_ok=True)
    
    credentials_file = os.path.join(youtube_dir, CREDENTIALS_FILENAME)
    token_file = os.path.join(youtube_dir, TOKEN_FILENAME)
    
    return credentials_file, token_file

def authenticate_youtube(credentials_file: str, token_file: str) -> Tuple[bool, str]:
    """Authenticate with YouTube API.
    
    Args:
        credentials_file: Path to the client_secret.json file
        token_file: Path to store the token.json file
        
    Returns:
        Tuple[bool, str]: (success, message)
    """
    try:
        # Check if credentials file exists
        if not os.path.exists(credentials_file):
            return False, f"Credentials file not found: {credentials_file}"
        
        creds = None
        
        # Check if token file exists
        if os.path.exists(token_file):
            try:
                with open(token_file, 'r') as token:
                    creds_data = json.load(token)
                    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(
                        creds_data, SCOPES)
            except Exception as e:
                logger.error(f"Error loading credentials from token file: {e}")
        
        # If credentials don't exist or are invalid, run the OAuth flow
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.error(f"Error refreshing credentials: {e}")
                    creds = None
            
            # If still no valid credentials, run the flow
            if not creds:
                # Try with trailing slash first (most common configuration)
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        credentials_file, SCOPES)
                    # Use explicit redirect URI configuration with trailing slash
                    flow.redirect_uri = "http://localhost:8080/"
                    creds = flow.run_local_server(port=8080)
                except Exception as first_error:
                    logger.error(f"First OAuth attempt failed with trailing slash: {first_error}")
                    
                    # Try without trailing slash
                    try:
                        flow = InstalledAppFlow.from_client_secrets_file(
                            credentials_file, SCOPES)
                        # Use explicit redirect URI configuration without trailing slash
                        flow.redirect_uri = "http://localhost:8080"
                        creds = flow.run_local_server(port=8080)
                    except Exception as second_error:
                        error_msg = str(second_error)
                        logger.error(f"Second OAuth attempt failed without trailing slash: {second_error}")
                        
                        # Provide detailed error message
                        if "redirect_uri_mismatch" in error_msg:
                            return False, (
                                "Redirect URI mismatch error. Please add ALL of the following redirect URIs to your OAuth client in Google Cloud Console:\n"
                                "- http://localhost:8080/ (with trailing slash)\n"
                                "- http://localhost:8080 (without trailing slash)\n"
                                "- http://127.0.0.1:8080/ (with trailing slash)\n"
                                "- http://127.0.0.1:8080 (without trailing slash)\n\n"
                                "Steps:\n"
                                "1. Go to Google Cloud Console > APIs & Services > Credentials\n"
                                "2. Edit your OAuth 2.0 Client ID\n"
                                "3. Add these URIs to the 'Authorized redirect URIs' section\n"
                                "4. Click Save and try again"
                            )
                        elif "invalid_client" in error_msg:
                            return False, "Invalid client error. Please check that your credentials file is correct and not corrupted."
                        elif "access_denied" in error_msg:
                            return False, "Access denied. You declined to authorize the application."
                        
                        return False, f"Authentication failed: {error_msg}"
            
            # Save the credentials for the next run
            try:
                os.makedirs(os.path.dirname(token_file), exist_ok=True)
                with open(token_file, 'w') as token:
                    token.write(creds.to_json())
            except Exception as e:
                logger.error(f"Error saving credentials to token file: {e}")
                return False, f"Failed to save token: {str(e)}"
        
        # Test the credentials by building the service
        try:
            youtube = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
            
            # Instead of checking channels, just verify we can get upload status
            # which is compatible with the upload scope
            try:
                # Simple API call that works with upload scope
                upload_status = youtube.videos().getRating(id="dQw4w9WgXcQ").execute()
                logger.info("YouTube API client created successfully")
                return True, "Successfully authenticated with YouTube"
            except HttpError as e:
                # Even if we get a 403 for this specific video, the API connection works
                if e.resp.status in [403, 404]:
                    logger.info("YouTube API client created successfully (with expected permission error)")
                    return True, "Successfully authenticated with YouTube"
                raise
            
        except Exception as e:
            logger.error(f"Error creating YouTube API client: {e}")
            return False, f"Failed to connect to YouTube API: {str(e)}"
            
    except Exception as e:
        logger.error(f"Unexpected error during authentication: {e}")
        return False, f"Authentication error: {str(e)}"

class YouTubeUploader:
    """Class to handle YouTube uploads."""
    
    def __init__(self, credentials_file: str, token_file: str):
        """Initialize the YouTube uploader.
        
        Args:
            credentials_file: Path to the client_secret.json file
            token_file: Path to store the token.json file
        """
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.youtube = None
    
    def authenticate(self) -> bool:
        """Authenticate with YouTube API.
        
        Returns:
            bool: True if authentication was successful, False otherwise
        """
        success, _ = authenticate_youtube(self.credentials_file, self.token_file)
        if success:
            creds = None
            with open(self.token_file, 'r') as token:
                creds_data = json.load(token)
                creds = google.oauth2.credentials.Credentials.from_authorized_user_info(
                    creds_data, SCOPES)
            self.youtube = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
            return True
        return False
    
    def upload_video(self, video_path: str, title: str, description: str, 
                     tags: Optional[List[str]] = None, 
                     privacy_status: str = "unlisted",
                     playlist_id: Optional[str] = None) -> Optional[str]:
        """Upload a video to YouTube.
        
        Args:
            video_path: Path to the video file
            title: Video title
            description: Video description
            tags: List of tags for the video
            privacy_status: Privacy status (private, unlisted, public)
            playlist_id: ID of playlist to add video to (optional)
            
        Returns:
            str: Video ID if upload was successful, None otherwise
        """
        if not self.youtube:
            if not self.authenticate():
                logger.error("Failed to authenticate with YouTube API")
                return None
        
        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            return None
        
        # Get file size for progress tracking
        file_size = os.path.getsize(video_path)
        logger.info(f"Uploading {os.path.basename(video_path)} ({file_size / (1024*1024):.1f} MB)")
        
        try:
            # Prepare the request body
            body = {
                'snippet': {
                    'title': title,
                    'description': description,
                    'tags': tags or [],
                    'categoryId': '17'  # Sports category
                },
                'status': {
                    'privacyStatus': privacy_status,
                    'selfDeclaredMadeForKids': False
                }
            }
            
            # Create the media upload object
            media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
            
            # Create the upload request
            request = self.youtube.videos().insert(
                part=','.join(body.keys()),
                body=body,
                media_body=media
            )
            
            # Execute the upload with progress tracking
            response = None
            error = None
            retry = 0
            
            while response is None:
                try:
                    status, response = request.next_chunk()
                    if status:
                        progress = int(status.progress() * 100)
                        logger.info(f"Upload progress: {progress}%")
                except HttpError as e:
                    if e.resp.status in [500, 502, 503, 504]:
                        # Recoverable error, retry
                        retry += 1
                        if retry > 5:
                            logger.error(f"Too many retries, giving up: {e}")
                            return None
                        logger.warning(f"Recoverable error {e.resp.status}, retrying ({retry}/5)")
                        time.sleep(2 ** retry)
                    else:
                        # Non-recoverable error
                        logger.error(f"Upload failed with error: {e}")
                        return None
                except Exception as e:
                    logger.error(f"Unexpected error during upload: {e}")
                    return None
            
            if response:
                video_id = response['id']
                logger.info(f"Successfully uploaded video: {title} (ID: {video_id})")
                
                # Add to playlist if specified
                if playlist_id:
                    self.add_video_to_playlist(video_id, playlist_id)
                
                return video_id
            else:
                logger.error("Upload completed but no response received")
                return None
                
        except Exception as e:
            logger.error(f"Error uploading video {video_path}: {e}")
            return None
    
    def find_playlist_by_name(self, name: str) -> Optional[str]:
        """Find a playlist by name.
        
        Args:
            name: Name of the playlist to find
            
        Returns:
            str: Playlist ID if found, None otherwise
        """
        if not self.youtube:
            if not self.authenticate():
                logger.error("Failed to authenticate with YouTube API")
                return None
        
        try:
            # Get all playlists for the authenticated user
            request = self.youtube.playlists().list(
                part="snippet",
                mine=True,
                maxResults=50
            )
            
            while request:
                response = request.execute()
                
                for playlist in response['items']:
                    if playlist['snippet']['title'] == name:
                        playlist_id = playlist['id']
                        logger.info(f"Found playlist '{name}' with ID: {playlist_id}")
                        return playlist_id
                
                # Check if there are more results
                request = self.youtube.playlists().list_next(request, response)
            
            logger.info(f"Playlist '{name}' not found")
            return None
            
        except Exception as e:
            logger.error(f"Error searching for playlist '{name}': {e}")
            return None
    
    def create_playlist(self, name: str, description: str = "", privacy_status: str = "unlisted") -> Optional[str]:
        """Create a new playlist.
        
        Args:
            name: Name of the playlist
            description: Description of the playlist
            privacy_status: Privacy status (private, unlisted, public)
            
        Returns:
            str: Playlist ID if created successfully, None otherwise
        """
        if not self.youtube:
            if not self.authenticate():
                logger.error("Failed to authenticate with YouTube API")
                return None
        
        try:
            request = self.youtube.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "title": name,
                        "description": description
                    },
                    "status": {
                        "privacyStatus": privacy_status
                    }
                }
            )
            
            response = request.execute()
            playlist_id = response['id']
            logger.info(f"Created playlist '{name}' with ID: {playlist_id}")
            return playlist_id
            
        except Exception as e:
            logger.error(f"Error creating playlist '{name}': {e}")
            return None
    
    def get_or_create_playlist(self, name: str, description: str = "", privacy_status: str = "unlisted") -> Optional[str]:
        """Get an existing playlist by name or create a new one.
        
        Args:
            name: Name of the playlist
            description: Description for the playlist if it needs to be created
            privacy_status: Privacy status for the playlist if it needs to be created
            
        Returns:
            str: Playlist ID if found or created successfully, None otherwise
        """
        playlist_id = self.find_playlist_by_name(name)
        if playlist_id:
            logger.info(f"Found existing playlist: {name} (ID: {playlist_id})")
            return playlist_id
        
        # Playlist not found, create it
        return self.create_playlist(name, description, privacy_status)
    
    def add_video_to_playlist(self, video_id: str, playlist_id: str) -> bool:
        """Add a video to a playlist.
        
        Args:
            video_id: The ID of the video to add
            playlist_id: The ID of the playlist to add the video to
            
        Returns:
            bool: True if the video was added successfully, False otherwise
        """
        if not self.youtube:
            if not self.authenticate():
                logger.error("Failed to authenticate with YouTube API")
                return False
        
        try:
            # Add the video to the playlist
            request = self.youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id
                        }
                    }
                }
            )
            
            response = request.execute()
            logger.info(f"Added video {video_id} to playlist {playlist_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error adding video to playlist: {e}")
            return False

def format_video_title(match_info, file_path: str, group_dir: str) -> str:
    """Format the video title according to the specified format.
    
    Args:
        match_info: MatchInfo object containing team names and location
        file_path: Path to the video file
        group_dir: Path to the group directory
        
    Returns:
        str: Formatted video title
    """
    # Extract date from group directory name (format: YYYY.MM.DD-HH.MM.SS)
    try:
        dir_name = os.path.basename(group_dir)
        date_part = dir_name.split('-')[0]  # YYYY.MM.DD
        date_obj = datetime.strptime(date_part, "%Y.%m.%d")
        formatted_date = date_obj.strftime("%m-%d-%Y")
    except Exception:
        # Fallback to current date if parsing fails
        formatted_date = datetime.now().strftime("%m-%d-%Y")
    
    # Base title format: "<my_team_name> vs <opponent_team_name> (<location>) MM-DD-YYYY"
    base_title = f"{match_info.my_team_name} vs {match_info.opponent_team_name} ({match_info.location}) {formatted_date}"
    
    # Check if this is a raw file
    if "-raw.mp4" in file_path:
        return f"{base_title} raw"
    
    return base_title

def get_playlist_name_from_mapping(team_name: str, config: configparser.ConfigParser) -> Optional[str]:
    """
    Get the base playlist name for a team from the config mapping.

    Args:
        team_name: The name of the team.
        config: The application config.

    Returns:
        The base playlist name, or None if not found.
    """
    if not config.has_section('YOUTUBE_PLAYLIST_MAPPING'):
        return None
    
    mapping = config['YOUTUBE_PLAYLIST_MAPPING']
    return mapping.get(team_name) or mapping.get('Default')

def upload_group_videos(group_dir: str, credentials_file: str, token_file: str,
                        processed_playlist_name: Optional[str] = None,
                        raw_playlist_name: Optional[str] = None,
                        privacy_status: str = "private") -> bool:
    """
    Uploads all videos in a group directory to YouTube.
    
    This function only handles the actual YouTube upload process.
    Playlist coordination and user interaction should be handled by the caller.

    Args:
        group_dir: The directory containing the videos and match_info.ini.
        credentials_file: Path to the YouTube API credentials file.
        token_file: Path to the YouTube API token file.
        processed_playlist_name: Name of playlist for processed videos (optional).
        raw_playlist_name: Name of playlist for raw videos (optional).
        privacy_status: Privacy status for uploaded videos.

    Returns:
        True if all uploads were successful, False otherwise.
    """
    match_info_path = os.path.join(group_dir, "match_info.ini")
    if not os.path.exists(match_info_path):
        logger.error(f"match_info.ini not found in {group_dir}")
        return False

    match_info = MatchInfo.from_file(match_info_path)
    if not match_info:
        logger.error(f"Could not load match info from {match_info_path}")
        return False

    uploader = YouTubeUploader(credentials_file, token_file)

    # Processed (trimmed) video
    processed_video_path = os.path.join(group_dir, "combined_trimmed.mp4")
    if os.path.exists(processed_video_path):
        logger.info(f"Uploading processed video: {processed_video_path}")
        title = match_info.get_youtube_title('processed')
        description = match_info.get_youtube_description('processed')
        playlist_id = None
        
        if processed_playlist_name:
            playlist_id = uploader.get_or_create_playlist(processed_playlist_name, description)
        
        success = uploader.upload_video(
            processed_video_path,
            title,
            description,
            privacy_status=privacy_status,
            playlist_id=playlist_id
        )
        
        if not success:
            logger.error(f"Failed to upload processed video: {processed_video_path}")
            return False

    # Raw (untrimmed) video
    raw_video_path = os.path.join(group_dir, "combined.mp4")
    if os.path.exists(raw_video_path):
        logger.info(f"Uploading raw video: {raw_video_path}")
        title = match_info.get_youtube_title('raw')
        description = match_info.get_youtube_description('raw')
        playlist_id = None

        if raw_playlist_name:
            playlist_id = uploader.get_or_create_playlist(raw_playlist_name, description)

        success = uploader.upload_video(
            raw_video_path,
            title,
            description,
            privacy_status=privacy_status,
            playlist_id=playlist_id
        )
        
        if not success:
            logger.error(f"Failed to upload raw video: {raw_video_path}")
            return False

    return True 