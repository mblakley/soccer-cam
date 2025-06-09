import os
import re
import subprocess
import sys
import httpx
import asyncio
import logging
from datetime import datetime, timedelta
import aiofiles
import configparser
import json
from typing import List, Tuple

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Change to DEBUG for more verbose logs
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)  # Ensure logs are written to stdout
    ]
)
logger = logging.getLogger(__name__)

# Load configuration
config = configparser.ConfigParser()
config.read("config.ini")

cameraConfig = config["CAMERA"]
DEVICE_IP = cameraConfig["ip_address"]
AUTH_USERNAME = cameraConfig["username"]
AUTH_PASSWORD = cameraConfig["password"]

default_date_format = "%Y-%m-%d %H:%M:%S"
LATEST_VIDEO_FILE = "latest_video.txt"
STATUS_FILE = "processing_status.txt"
MATCH_INFO_TEMPLATE = "match_info.ini.dist"
MATCH_INFO_FILE = "match_info.ini"

STATES = ["downloading", "combining", "user_input", "post_processing", "finished"]

# Locks to ensure only one download and one ffmpeg execution at a time
download_lock = asyncio.Lock()
ffmpeg_lock = asyncio.Lock()

CAMERA_EVENTS_FILE = "camera_events.json"

class CameraEvent:
    def __init__(self, event_type: str, timestamp: datetime):
        self.event_type = event_type  # "plug" or "unplug"
        self.timestamp = timestamp

    def to_dict(self):
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp.strftime(default_date_format)
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CameraEvent":
        return cls(
            data["event_type"],
            datetime.strptime(data["timestamp"], default_date_format)
        )

def load_camera_events() -> List[CameraEvent]:
    if not os.path.exists(CAMERA_EVENTS_FILE):
        return []
    
    try:
        with open(CAMERA_EVENTS_FILE, 'r') as f:
            events_data = json.load(f)
            return [CameraEvent.from_dict(event) for event in events_data]
    except Exception as e:
        logger.error(f"Error loading camera events: {e}")
        return []

def save_camera_events(events: List[CameraEvent]):
    try:
        with open(CAMERA_EVENTS_FILE, 'w') as f:
            json.dump([event.to_dict() for event in events], f, indent=2)
    except Exception as e:
        logger.error(f"Error saving camera events: {e}")

def get_time_windows(events: List[CameraEvent]) -> List[Tuple[datetime, datetime]]:
    windows = []
    for i in range(len(events) - 1):
        if events[i].event_type == "unplug" and events[i + 1].event_type == "plug":
            windows.append((events[i].timestamp, events[i + 1].timestamp))
    return windows

class RecordingFile:
    def __init__(self, start_time: datetime, end_time: datetime, file_path: str):
        self.start_time = start_time
        self.end_time = end_time
        self.file_path = file_path
    
    @classmethod
    def from_response(cls, response_text: str) -> list["RecordingFile"]:
        files = []
        lines = response_text.split("\n")
        current_file = {}

        for line in lines:
            if line.startswith("items["):
                key, value = line.split("=")
                key = key.strip()
                value = value.strip()
                
                if ".StartTime" in key:
                    current_file["start_time"] = datetime.strptime(value, default_date_format)
                elif ".EndTime" in key:
                    current_file["end_time"] = datetime.strptime(value, default_date_format)
                elif ".FilePath" in key:
                    current_file["file_path"] = value
                
                if len(current_file) == 3:  # If we have all necessary values
                    files.append(cls(current_file["start_time"], current_file["end_time"], current_file["file_path"]))
                    current_file = {}  # Reset for next file entry

        return sorted(files, key=lambda x: x.start_time)

def update_status(directory, state):
    if state not in STATES:
        raise ValueError("Invalid state")
    status_file = os.path.join(directory, STATUS_FILE)
    
    with open(status_file, "w") as f:
        f.write(state)

    logger.info(f"Updated status to {state} in {status_file}")

def get_status(directory):
    status_file = os.path.join(directory, STATUS_FILE)
    if os.path.exists(status_file):
        with open(status_file, "r") as f:
            return f.read().strip()
    return None

def parse_match_info(file_path):
    match_info_config = configparser.ConfigParser()
    match_info_config.read(file_path)
    return match_info_config["MATCH"] if "MATCH" in match_info_config else None

def create_directory(path):
    os.makedirs(path, exist_ok=True)

async def make_http_request(url: str, auth: httpx.DigestAuth):
    async with httpx.AsyncClient() as client:
        return await client.get(url, auth=auth)

async def run_ffmpeg(command):
    async with ffmpeg_lock:
        try:
            subprocess.run(command, check=True, text=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg command failed: {e}")
            logger.error(f"Error output: {e.stderr}")

async def convert_davs_to_mp4(directory_path):
    for input_file in os.listdir(directory_path):
        if input_file.endswith(".dav"):
            input_file_path = os.path.join(directory_path, input_file)
            output_file_path = input_file_path.replace(".dav", ".mp4")
            if not os.path.exists(output_file_path):
                command = ["ffmpeg", "-i", input_file_path, "-vcodec", "copy", "-acodec", "alac", "-threads", "0", "-async", "1", output_file_path]
                logger.info(f"Converting: {input_file_path} -> {output_file_path}")
                await run_ffmpeg(command)  # Ensure we await ffmpeg execution

def all_fields_filled(match_info):
    required_fields = ["start_time_offset", "my_team_name", "opponent_team_name", "location"]
    return all(match_info.get(field) for field in required_fields)

async def concatenate_videos(directory):
    output_file = os.path.join(directory, "combined.mp4")
    list_file = os.path.join(directory, "video_list.txt")
    
    with open(list_file, "w") as f:
        for file in sorted(os.listdir(directory)):
            if file.endswith(".mp4"):
                f.write(f"file '{os.path.join(directory, file)}'\n")

    if not os.path.exists(list_file):
        logger.error(f"Unable to combine videos: missing {list_file}")
        raise ValueError(f"Missing {list_file}")

    logger.info(f"Combining videos in {directory}")
    command = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output_file]
    await run_ffmpeg(command)

async def process_files():
    while True:
        try:
            storage_path = config["APP"]["video_storage_path"]
            logger.info(f"Processing files in {storage_path}")
            for directory in os.listdir(storage_path):
                full_path = os.path.join(storage_path, directory)
                if not os.path.isdir(full_path):
                    continue
                
                status = get_status(full_path)
                if status == "combining":
                    await convert_davs_to_mp4(full_path)
                    await concatenate_videos(full_path)
                    match_info_path = os.path.join(full_path, MATCH_INFO_FILE)
                    if not os.path.exists(MATCH_INFO_TEMPLATE):
                        logger.info(f"Missing match info template: {MATCH_INFO_TEMPLATE}")
                        continue

                    if not os.path.exists(match_info_path):
                        with open(MATCH_INFO_TEMPLATE, "r") as template, open(match_info_path, "w") as match_info:
                            match_info.write(template.read())
                    update_status(full_path, "user_input")
                
                elif status == "user_input":
                    match_info = configparser.ConfigParser()
                    match_info_path = os.path.join(full_path, MATCH_INFO_FILE)
                    if not os.path.exists(match_info_path):
                        logger.info(f"No match info file exists in {full_path}")
                        continue

                    match_info.read(match_info_path)
                    if all_fields_filled(match_info["MATCH"]):
                        update_status(full_path, "post_processing")
                    else:
                        logger.info(f"Waiting for match info in {full_path}")

                elif status == "post_processing":
                    match_info_path = os.path.join(full_path, MATCH_INFO_FILE)
                    if not os.path.exists(match_info_path):
                        logger.info(f"Skipping post-processing: {match_info_path} missing")
                        continue
                    
                    match_info = configparser.ConfigParser()
                    match_info.read(match_info_path)
                    if "MATCH" not in match_info:
                        logger.info(f"Skipping post-processing: Invalid {match_info_path}")
                        continue

                    await trim_video(full_path, match_info["MATCH"])
        except Exception as e:
            logger.error(f"Unexpected error processing files: {e}")
        
        await asyncio.sleep(config["APP"].getint("check_interval_seconds"))

def cleanup_dav_files(directory):
    dav_files = [f for f in os.listdir(directory) if f.endswith(".dav")]
    for file in dav_files:
        file_path = os.path.join(directory, file)
        try:
            os.remove(file_path)
        except Exception as e:
            logger.error(f"Error deleting {file}: {e}")
    
    logger.info("Dav file cleanup complete.")

async def trim_video(directory, match_info):
    logger.info("Trimming and renaming video...")
    combined_file = os.path.join(directory, "combined.mp4")
    if not os.path.exists(combined_file):
        logger.info(f"Skipping trim: Missing {combined_file}")
        return

    dir_date = os.path.basename(directory).split('-')[0]
    formatted_date = datetime.strptime(dir_date, "%Y.%m.%d").strftime("%m-%d-%Y")
    output_dir = os.path.join(directory, f"{dir_date} - {match_info['my_team_name']} vs {match_info['opponent_team_name']} ({str(match_info['location'])})")
    create_directory(output_dir)

    # Validate start_time_offset
    start_time_offset = match_info.get("start_time_offset", "").strip()
    if not start_time_offset:
        logger.info(f"Skipping trim: Missing start_time_offset in {directory}")
        return

    output_file = os.path.join(
        output_dir,
        f"{match_info['my_team_name'].lower().replace(' ', '')}-"
        f"{match_info['opponent_team_name'].lower().replace(' ', '')}-"
        f"{match_info['location'].lower().replace(' ', '')}-{formatted_date}-raw.mp4"
    )

    logger.info(f"Trimming {output_file} starting at {start_time_offset}")
    command = ["ffmpeg", "-y", "-i", combined_file, "-ss", start_time_offset, "-c", "copy", output_file]
    await run_ffmpeg(command)

    cleanup_dav_files(directory)
    update_status(directory, "finished")

async def check_device_availability(auth) -> bool:
    try:
        device_check_url = f"http://{DEVICE_IP}/cgi-bin/recordManager.cgi?action=getCaps"
        logger.info(f"Checking for camera devices available on network: {device_check_url}")
        response = await make_http_request(device_check_url, auth=auth)
        
        events = load_camera_events()
        current_time = datetime.now()
        
        if response.status_code == 200:
            # Camera is plugged in
            if not events or events[-1].event_type != "plug":
                events.append(CameraEvent("plug", current_time))
                logger.info(f"Camera plugged in at {current_time}")
                save_camera_events(events)
            return True
        else:
            logger.info(f"Received response from camera, but query was not successful.  Status Code: {response.status_code}")
            return False
    except Exception as e:
        logger.info(f"Camera device was not found at {DEVICE_IP}: {e}")
        # Camera is unplugged
        events = load_camera_events()
        if events and events[-1].event_type == "plug":
            events.append(CameraEvent("unplug", datetime.now()))
            logger.info(f"Camera unplugged at {events[-1].timestamp}")
            save_camera_events(events)
        return False

def get_subdirectory_time_ranges(storage_path):
    time_ranges = []

    # Regex pattern to match subdirectory names (YYYY.MM.DD)
    dir_pattern = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")

    # Regex pattern to match filenames with time ranges (HH.MM.SS-HH.MM.SS)
    file_pattern = re.compile(r"(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})")

    for dirname in sorted(os.listdir(storage_path)):  # Ensure directories are processed in order
        dir_match = dir_pattern.match(dirname)
        if not dir_match:
            continue  # Skip non-matching directories

        year, month, day = map(int, dir_match.groups())
        dir_path = os.path.join(storage_path, dirname)

        if not os.path.isdir(dir_path):
            continue  # Skip files, only process directories

        start_time = None
        end_time = None

        for filename in sorted(os.listdir(dir_path)):  # Ensure files are processed in order
            file_match = file_pattern.match(filename)
            if not file_match:
                continue  # Skip non-matching files

            # Extract times from filename
            start_h, start_m, start_s, end_h, end_m, end_s = map(int, file_match.groups())

            # Convert to datetime for proper sorting
            file_start_time = datetime(year, month, day, start_h, start_m, start_s)
            file_end_time = datetime(year, month, day, end_h, end_m, end_s)

            # Update start and end times
            if start_time is None or file_start_time < start_time:
                start_time = file_start_time
            if end_time is None or file_end_time > end_time:
                end_time = file_end_time

        if start_time and end_time:
            time_ranges.append((start_time, end_time, dirname))

    return time_ranges

async def stop_recording(auth) -> bool:
    try:
        stop_url = f"http://{DEVICE_IP}/cgi-bin/configManager.cgi?action=setConfig&RecordMode[0].Mode=2"
        response = await make_http_request(stop_url, auth=auth)
        if response.status_code == 200 and response.text.strip() == "OK":
            logger.info("Successfully stopped recording")
            return True
        else:
            logger.error(f"Failed to stop recording. Status code: {response.status_code}, Response: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error stopping recording: {e}")
        return False

async def find_and_download_files(auth):
    async with download_lock:
        events = load_camera_events()
        if len(events) < 2:
            logger.info("Skipping download - waiting for both unplug and plug events")
            return

        # Get all time windows that need processing
        time_windows = get_time_windows(events)
        if not time_windows:
            logger.info("No time windows to process")
            return

        # Process each time window
        for start_time, end_time in time_windows:
            response = await make_http_request(f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=factory.create", auth=auth)
            if response.status_code != 200:
                logger.info("Failed to create media file finder factory.")
                continue

            object_id = response.text.split('=')[1].strip()
            latest_file_path = os.path.join(config["APP"]["video_storage_path"], LATEST_VIDEO_FILE)

            if os.path.exists(latest_file_path):
                with open(latest_file_path, "r") as latest_file:
                    latest_video_timestamp = latest_file.read().strip()
                    window_start = datetime.strptime(latest_video_timestamp, default_date_format)
            else:
                window_start = start_time.replace(hour=0, minute=0, second=0, microsecond=0)
            
            start_time_formatted = window_start.strftime("%Y-%m-%d%%20%H:%M:%S")
            end_time_formatted = end_time.strftime("%Y-%m-%d%%20%H:%M:%S")

            logger.info(f"Searching for files between {window_start} and {end_time}")

            findfile_url = f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=findFile&object={object_id}&condition.Channel=1&condition.Types[0]=dav&condition.StartTime={start_time_formatted}&condition.EndTime={end_time_formatted}&condition.VideoStream=Main"
            response = await make_http_request(findfile_url, auth=auth)
            if response.status_code != 200:
                logger.info("Failed to find media files.")
                continue

            response = await make_http_request(f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={object_id}&count=100", auth=auth)
            if response.status_code != 200:
                logger.info("Failed to retrieve media file list.")
                continue

            files = RecordingFile.from_response(response.text)
            if not files:
                # If no files found in this window, we can clean up the events
                events = [e for e in events if e.timestamp > end_time]
                save_camera_events(events)
                continue

            # Process files as before...
            storage_path = config["APP"]["video_storage_path"]
            current_group_dir = None

            for file in files:
                filename = file.file_path.split("/")[-1]

                # Get existing directories and their time ranges
                existing_ranges = get_subdirectory_time_ranges(storage_path)

                # Determine the appropriate directory for this file
                assigned_directory = None
                for start_time, end_time, dirname in existing_ranges:
                    # If the file's start time is within 60 seconds of an existing range, add it to that directory
                    if end_time and 0 <= (file.start_time - end_time).total_seconds() <= 60:
                        assigned_directory = os.path.join(storage_path, dirname)
                        logger.info(f"📂 Adding {filename} to existing directory: {dirname}")
                        break

                # If no suitable existing directory, create a new one
                if assigned_directory is None:
                    has_dav_files = any(f.endswith(".dav") for f in os.listdir(current_group_dir)) if current_group_dir else False
                    if current_group_dir and has_dav_files:
                        logger.info(f"Done downloading files to {current_group_dir}")
                        update_status(current_group_dir, "combining")  # Mark previous dir as done

                    assigned_directory = os.path.join(storage_path, file.start_time.strftime("%Y.%m.%d-%H.%M.%S"))
                    create_directory(assigned_directory)
                    logger.info(f"📁 Creating new directory: {assigned_directory}")

                current_group_dir = assigned_directory  # Update current directory for future files
                
                # Download file if it doesn't already exist
                full_download_path = os.path.join(current_group_dir, filename)
                if not os.path.exists(full_download_path):
                    update_status(current_group_dir, "downloading")
                    try:
                        async with httpx.AsyncClient() as client:
                            async with client.stream("GET", f"http://{DEVICE_IP}/cgi-bin/RPC_Loadfile{file.file_path}", auth=auth, timeout=1200.0, follow_redirects=True) as file_response:
                                if file_response.status_code == 200:
                                    async with aiofiles.open(full_download_path, "wb") as file_handle:
                                        async for chunk in file_response.aiter_bytes():
                                            await file_handle.write(chunk)
                                    logger.info(f"✅ Successfully downloaded {file.file_path} to {full_download_path}")
                                    with open(latest_file_path, "w") as latest_file:
                                        latest_file.write(file.end_time.strftime(default_date_format))
                                else:
                                    logger.info(f"❌ Download failed: {file.file_path} (HTTP {file_response.status_code})")
                    except httpx.HTTPStatusError as e:
                        logger.error(f"❌ HTTP error downloading {file.file_path}: {e}")
                    except httpx.TimeoutException:
                        logger.error(f"⚠️ Timeout while downloading {file.file_path}")
                    except Exception as e:
                        logger.error(f"❌ Error downloading {file.file_path}: {e}")
                else:
                    logger.info(f"🟡 Skipping {file.file_path} (already exists)")

async def download_files():
    auth = httpx.DigestAuth(AUTH_USERNAME, AUTH_PASSWORD)
    while True:
        try:
            logger.info("Checking for new files...")
            if await check_device_availability(auth):
                await find_and_download_files(auth)
        except Exception as e:
            logger.error(f"Unexpected error downloading files: {e}")
        
        await asyncio.sleep(config["APP"].getint("check_interval_seconds"))


async def main():
    try:
        await asyncio.gather(download_files(), process_files())
    except asyncio.CancelledError:
        logger.error("Shutting down gracefully...")

if __name__ == "__main__":
    logger.info("Starting video processing...")
    asyncio.run(main())
