import os
import logging
import json
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import google.oauth2.credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# If modifying these scopes, delete the token.json file.
SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.readonly'
]
API_SERVICE_NAME = 'youtube'
API_VERSION = 'v3'

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
                     privacy_status: str = "unlisted") -> Optional[str]:
        """Upload a video to YouTube.
        
        Args:
            video_path: Path to the video file
            title: Title of the video
            description: Description of the video
            tags: List of tags for the video
            privacy_status: Privacy status of the video (private, unlisted, public)
            
        Returns:
            str: YouTube video ID if upload was successful, None otherwise
        """
        if not self.youtube:
            if not self.authenticate():
                logger.error("Failed to authenticate with YouTube API")
                return None
        
        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            return None
        
        if tags is None:
            tags = []
        
        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': tags,
                'categoryId': '17'  # Sports category
            },
            'status': {
                'privacyStatus': privacy_status,
                'selfDeclaredMadeForKids': False
            }
        }
        
        try:
            # Create a MediaFileUpload object for the video file
            media = MediaFileUpload(
                video_path, 
                mimetype='video/mp4', 
                resumable=True
            )
            
            # Call the API's videos.insert method to upload the video
            insert_request = self.youtube.videos().insert(
                part=','.join(body.keys()),
                body=body,
                media_body=media
            )
            
            logger.info(f"Starting upload for {os.path.basename(video_path)}")
            
            # Upload the video with progress tracking
            response = None
            while response is None:
                status, response = insert_request.next_chunk()
                if status:
                    percent = int(status.progress() * 100)
                    logger.info(f"Upload progress: {percent}%")
            
            logger.info(f"Upload complete for {os.path.basename(video_path)}")
            video_id = response['id']
            logger.info(f"Video ID: {video_id}")
            return video_id
            
        except HttpError as e:
            logger.error(f"An HTTP error occurred: {e.resp.status} {e.content}")
            return None
        except Exception as e:
            logger.error(f"An error occurred during upload: {e}")
            return None

def upload_group_videos(group_dir: str, credentials_file: str, token_file: str) -> bool:
    """Upload all videos from a group directory to YouTube.
    
    Args:
        group_dir: Path to the group directory
        credentials_file: Path to the client_secret.json file
        token_file: Path to store the token.json file
        
    Returns:
        bool: True if all uploads were successful, False otherwise
    """
    group_path = Path(group_dir)
    
    # Check if the group directory exists
    if not group_path.exists() or not group_path.is_dir():
        logger.error(f"Group directory not found: {group_dir}")
        return False
    
    # Check if state.json exists and status is autocam_complete
    state_file = group_path / "state.json"
    if not state_file.exists():
        logger.error(f"State file not found in group directory: {group_dir}")
        return False
    
    try:
        with open(state_file, 'r') as f:
            state_data = json.load(f)
        
        if state_data.get('status') != 'autocam_complete':
            logger.error(f"Group status is not autocam_complete: {state_data.get('status')}")
            return False
    except Exception as e:
        logger.error(f"Error reading state file: {e}")
        return False
    
    # Find the raw and processed video files
    raw_file = None
    processed_file = None
    
    for file in group_path.glob('**/*-raw.mp4'):
        raw_file = file
        break
    
    if raw_file:
        # The processed file should have the same name but without the -raw suffix
        processed_file = raw_file.with_name(raw_file.name.replace('-raw.mp4', '.mp4'))
        if not processed_file.exists():
            logger.error(f"Processed file not found: {processed_file}")
            processed_file = None
    else:
        logger.error(f"Raw file not found in group directory: {group_dir}")
        return False
    
    # Get match info for video metadata
    match_info_file = group_path / "match_info.ini"
    title_prefix = "Soccer Match"
    description = "Soccer match recording"
    
    if match_info_file.exists():
        try:
            import configparser
            from video_grouper.models import MatchInfo
            
            match_info = MatchInfo.from_file(str(match_info_file))
            if match_info:
                title_prefix = f"{match_info.my_team_name} vs {match_info.opponent_team_name}"
                description = f"Soccer match: {match_info.my_team_name} vs {match_info.opponent_team_name} at {match_info.location}"
        except Exception as e:
            logger.error(f"Error reading match info: {e}")
    
    # Create YouTube uploader
    uploader = YouTubeUploader(credentials_file, token_file)
    if not uploader.authenticate():
        logger.error("Failed to authenticate with YouTube API")
        return False
    
    success = True
    
    # Upload raw video if it exists
    if raw_file:
        raw_title = f"{title_prefix} - Raw Recording"
        raw_id = uploader.upload_video(
            str(raw_file),
            title=raw_title,
            description=f"{description} (Raw recording)",
            tags=["soccer", "raw footage"],
            privacy_status="unlisted"
        )
        if not raw_id:
            logger.error(f"Failed to upload raw video: {raw_file}")
            success = False
        else:
            logger.info(f"Successfully uploaded raw video: {raw_file} (ID: {raw_id})")
    
    # Upload processed video if it exists
    if processed_file:
        processed_title = f"{title_prefix} - Processed"
        processed_id = uploader.upload_video(
            str(processed_file),
            title=processed_title,
            description=f"{description} (Processed with Once Autocam)",
            tags=["soccer", "autocam"],
            privacy_status="unlisted"
        )
        if not processed_id:
            logger.error(f"Failed to upload processed video: {processed_file}")
            success = False
        else:
            logger.info(f"Successfully uploaded processed video: {processed_file} (ID: {processed_id})")
    
    # Update state if all uploads were successful
    if success:
        try:
            with open(state_file, 'r') as f:
                state_data = json.load(f)
            
            state_data['status'] = 'youtube_uploaded'
            
            with open(state_file, 'w') as f:
                json.dump(state_data, f, indent=4)
            
            logger.info(f"Updated group status to youtube_uploaded: {group_dir}")
        except Exception as e:
            logger.error(f"Error updating state file: {e}")
            success = False
    
    return success 