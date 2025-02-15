from datetime import datetime, timezone
import os
import requests
from onvif import ONVIFCamera
import onvif
from zeep.transports import Transport
from requests import Session

# Camera configuration
CAMERA_IP = "192.168.86.108"  # Change this to the correct IP
CAMERA_PORT = 80  # Default ONVIF port
CAMERA_USER = "admin"
CAMERA_PASS = "mblakley82"
DOWNLOAD_DIR = "./downloads"
LAST_DOWNLOAD_FILE = "last_download_time.txt"

# Create download directory if it doesn't exist
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def get_last_download_time():
    if os.path.exists(LAST_DOWNLOAD_FILE):
        with open(LAST_DOWNLOAD_FILE, "r") as f:
            return datetime.fromisoformat(f.read().strip()).replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)

def update_last_download_time(new_time):
    with open(LAST_DOWNLOAD_FILE, "w") as f:
        f.write(new_time.isoformat())

def connect_to_camera():
    try:
        wsdl_path = "/usr/local/lib/python3.10/site-packages/wsdl"
        print(f"Using ONVIF WSDL path: {wsdl_path}")
        
        session = Session()
        session.timeout = 10  # Set a timeout for ONVIF requests
        transport = Transport(session=session)
        
        camera = ONVIFCamera(CAMERA_IP, CAMERA_PORT, CAMERA_USER, CAMERA_PASS, wsdl_path, transport=transport)
        media_service = camera.create_media_service()
        try:
            search_service = camera.create_search_service()
            print("Search Service found and initialized.")
        except Exception:
            search_service = None
            print("Warning: Search Service is not available on this camera.")
        
        return media_service, search_service
    except Exception as e:
        print(f"Error connecting to camera: {e}")
        return None, None

def get_recordings(search_service):
    try:
        if not search_service:
            print("Search Service not available. Cannot retrieve recordings.")
            return []
        
        print("Attempting to find recordings...")
        search_token = search_service.FindRecordings()
        if not search_token:
            print("No recordings found.")
            return []

        print(f"Search token received: {search_token}")
        results = search_service.GetRecordingSearchResults(SearchToken=search_token)
        
        if results:
            print(f"Found {len(results)} recordings.")
        else:
            print("No recordings returned from search.")
        
        return results if results else []
    except Exception as e:
        print(f"Error fetching recordings: {e}")
        return []

def download_file(file_url, save_path):
    response = requests.get(file_url, stream=True)
    if response.status_code == 200:
        with open(save_path, "wb") as f:
            for chunk in response.iter_content(1024):
                f.write(chunk)
        print(f"Downloaded: {save_path}")
        return True
    print(f"Failed to download {file_url}")
    return False

def main():
    print("Starting video grouper")
    last_download_time = get_last_download_time()
    print(f"Last downloaded file timestamp: {last_download_time}")
    
    media_service, search_service = connect_to_camera()
    if not media_service:
        return

    recordings = get_recordings(search_service)
    if not recordings:
        print("No new recordings found.")
        return
    
    latest_time = last_download_time
    for recording in recordings:
        file_timestamp = datetime.strptime(recording.CreationTime, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        if file_timestamp > last_download_time:
            filename = os.path.join(DOWNLOAD_DIR, f"{file_timestamp.strftime('%Y-%m-%d_%H-%M-%S')}.mp4")
            if download_file(recording.Path, filename):
                latest_time = max(latest_time, file_timestamp)
    
    update_last_download_time(latest_time)
    print("Download complete.")

if __name__ == "__main__":
    main()
