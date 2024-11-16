import os
import subprocess
import textwrap
import httpx
import asyncio
from datetime import datetime, timedelta
import aiofiles
import configparser

config = configparser.ConfigParser()
config.read('config.ini')

cameraConfig = config['CAMERA']
DEVICE_IP = cameraConfig['ip_address']
AUTH_USERNAME = cameraConfig['username']
AUTH_PASSWORD = cameraConfig['password']

appConfig = config['APP']
CHECK_INTERVAL_SECONDS = appConfig.getint('check_interval_seconds')
STATUS_FILE_PATH = appConfig['status_file_path']
VIDEO_STORAGE_PATH = appConfig['video_storage_path']
TEAM_NAME = appConfig['team_name']
FINISHED_FILE = "finished.txt"
MATCH_INFO_FILE = "match_info.txt"


def trim_file_to_offset(combined_filename: str, match_info: dict):
    input_file = combined_filename
    directory_path = os.path.dirname(combined_filename)
    dir_date = os.path.basename(directory_path).split('-')[0]
    formatted_date = datetime.strptime(dir_date, "%Y.%m.%d").strftime("%m-%d-%Y")
    output_dir = f"{dir_date} - vs {match_info['opponent_team_name']} ({match_info['location']})"
    output_file = os.path.join(directory_path, output_dir, f"{match_info['my_team_name'].lower().replace(' ', '')}-{match_info['opponent_team_name'].lower().replace(' ', '')}-{match_info['location'].lower().replace(' ', '')}-{formatted_date}-raw")
    command = ["ffmpeg", "-i", input_file, "-ss", match_info['start_time_offset'], "-c", "copy", "-threads", "0", "-async", "1", output_file]
    print(f"input_file: {input_file} -> output_file: {output_file}.  Calling {command}")
    result = subprocess.run(command, capture_output=True, check=True)
    print(f"Completed conversion to mp4: {output_file}.  Result: {result}")

def process_all_files(group_directories: list[str]):
    # Filter to include only directories
    directories = [d for d in group_directories if os.path.isdir(os.path.join(VIDEO_STORAGE_PATH, d))]

    # Sort directories by modification time (newest first)
    sorted_directories = sorted(
        directories,
        key=lambda d: os.path.getmtime(os.path.join(VIDEO_STORAGE_PATH, d)),
        reverse=True
    )
    for group_dir in sorted_directories:
        group_full_path = os.path.join(VIDEO_STORAGE_PATH, group_dir)
        combined_filename = os.path.join(group_full_path, "combined.mp4")
        for file in os.listdir(group_full_path):
            if file.endswith(".dav") and not os.path.exists(os.path.join(group_full_path, file).replace(".dav", ".mp4")):
                # ffmpeg copy file to mp4
                input_file = os.path.join(group_full_path, file)
                output_file = input_file.replace(".dav", ".mp4")
                command = ["ffmpeg", "-i", input_file, "-vcodec", "copy", "-acodec", "alac", "-threads", "0", "-async", "1", output_file]
                print(f"input_file: {input_file} -> output_file: {output_file}.  Calling {command}")
                result = subprocess.run(command, capture_output=True, check=True)
                print(f"Completed conversion to mp4: {output_file}.  Result: {result}")
        combine_list_file = os.path.join(group_full_path, "output.txt")
        print(f"combine_list_file: {combine_list_file}")
        # remove any existing combination file so we don't end up duplicating entries
        if (os.path.exists(combine_list_file)):
            os.remove(combine_list_file)
        for file in os.listdir(group_full_path):
            if file.endswith(".mp4"):
                # append the filename to the list of files to combine
                with open(combine_list_file, 'a') as f:
                    mp_file_path = os.path.join(group_full_path, file)
                    print(f"Writing file to group concat: {mp_file_path} -> {combine_list_file}")
                    f.write(f"file '{mp_file_path}'\n")
        # ffmpeg combine files grouped by time
        print(f"combined_filename: {combined_filename}")
        if (os.path.exists(combined_filename)):
            os.remove(combined_filename)
        command = ["ffmpeg", "-f" ,"concat" ,"-safe", "0", "-i", combine_list_file, "-c", "copy", combined_filename]
        print(f"Combining mp4 files.  Calling {command}")
        subprocess.run(command, capture_output=True, check=True)
        print(f"Completed combination of mp4 files: {combined_filename }")
        empty_match_info = '''[MATCH]
        start_time_offset =
        my_team_name =
        opponent_team_name =
        location =
        '''
        with open(match_info_file, 'w') as f:
            f.write(textwrap.dedent(empty_match_info))
        print(f"Fill in the match_info.ini file in {group_full_path} to finish processing")
        # TODO: Additional processing to find and follow the ball
        # TODO: Auto upload to youtube

async def find_files_to_download(auth):
    async with httpx.AsyncClient() as client:
        updated_download_directories = []
        # There are more files to download, so download them
        response = await client.get(f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=factory.create", auth=auth)
        print(f"Response: {response.status_code}")
        if (response.status_code == 200):
            print(f"Created file finder: {response.text}")
            object_id = response.text.split('=')[1].strip()
            current_date_time = datetime.now()
            if not os.path.exists(STATUS_FILE_PATH):
                # Create the datetime string for midnight today
                midnight_today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                prev_recording_end_contents = midnight_today.strftime("%Y-%m-%d %H:%M:%S")
                # Write today at midnight datetime string to the new file
                with open(STATUS_FILE_PATH, 'w') as file:
                    file.write(prev_recording_end_contents)
            else:
                with open(STATUS_FILE_PATH, 'r') as file:
                    prev_recording_end_contents = file.read()
            date_format = "%Y-%m-%d %H:%M:%S"
            prev_recording_end = datetime.strptime(prev_recording_end_contents, date_format)
            start_time = prev_recording_end + timedelta(hours=2)
            end_time = current_date_time - timedelta(minutes=20)
            start_time_formatted = datetime.strftime(start_time, date_format).replace("-0", "-").replace(" ", "%20")
            end_time_formatted = datetime.strftime(end_time, date_format).replace("-0", "-").replace(" ", "%20")
            findfile_url = f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=findFile&object={object_id}&condition.Channel=1&condition.Types[0]=dav&condition.StartTime={start_time_formatted}&condition.EndTime={end_time_formatted}&condition.VideoStream=Main"
            print(f"findFile URL: {findfile_url}")
            # fileFind with StartTime set to previous startTime and EndTime set to now
            response = await client.get(findfile_url, auth=auth)
            print(f"findFile: {response.status_code}")
            # Expect OK
            if (response.status_code == 200):
                response = await client.get(f"http://{DEVICE_IP}/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={object_id}&count=100", auth=auth)
                print(f"findNextFile: {response.status_code}")
                if (response.status_code == 200):
                    print(f"findNextFile: {response.text}")
                    recent_start_time = None
                    recent_end_time = None
                    clip_duration = None
                    prev_clip_duration = 0
                    for line in response.text.split("\n"):
                        if (".EndTime" in line):
                            recent_end_time = datetime.strptime(line.split("=")[1].strip(), date_format)
                            with open(STATUS_FILE_PATH, 'w') as file:
                                file.write(recent_end_time.strftime("%Y-%m-%d %H:%M:%S"))
                            print(f"findNextFile endtime: {recent_end_time}")
                        if (".Duration" in line):
                            if (clip_duration):
                                prev_clip_duration = clip_duration
                            else:
                                prev_clip_duration = 0
                            clip_duration = int(line.split("=")[1].strip())
                            two_clips_duration = clip_duration + prev_clip_duration
                            print(f"findNextFile duration: {clip_duration}")
                        if (".StartTime" in line):
                            recent_start_time = datetime.strptime(line.split("=")[1].strip(), date_format)
                            print(f"findNextFile starttime: {recent_start_time}")
                        if (".FilePath" in line):
                            file_to_download = line.split("=")[1].strip()
                            print(f"findNextFile filepath: {file_to_download}")
                            downloaded_filename = file_to_download.split('/')[-1]
                            # Group files that start and end within 5 seconds of each other
                            if (recent_start_time and clip_duration and recent_end_time and ((recent_start_time + timedelta(seconds=two_clips_duration)) - recent_end_time).total_seconds() < 5):
                                print(f"Same group: {recent_start_time}")
                            else:
                                # If this recording is in a new group, create a new directory to download recordings into
                                # get start time before we actually get to the StartTime line
                                group_start_date = recent_end_time - timedelta(seconds=clip_duration)
                                print(f"New group: {group_start_date}")
                            downloaded_file_path = os.path.join(VIDEO_STORAGE_PATH, datetime.strftime(group_start_date, "%Y.%m.%d-%H.%M.%S"), downloaded_filename)
                            if (os.path.exists(downloaded_file_path)):
                                continue
                            download_directory = os.path.dirname(downloaded_file_path)
                            updated_download_directories.append(download_directory)
                            print(f"Download path: {downloaded_file_path}")
                            # Check if the directory exists
                            if not os.path.exists(download_directory):
                                # Create the directory
                                os.makedirs(download_directory)
                            # Figure out how to configure where to save the data to
                            loadfile_url = f"http://{DEVICE_IP}/cgi-bin/RPC_Loadfile{file_to_download}"
                            print(f"loadfile url: {loadfile_url}")
                            response = await client.get(loadfile_url, auth=auth)
                            print(f"RPC_Loadfile: {response.status_code}")
                            if (response.status_code == 200):
                                # Open the file and write chunks asynchronously
                                async with aiofiles.open(downloaded_file_path, 'wb') as file:
                                    async for chunk in response.aiter_bytes():
                                        await file.write(chunk)
                                # TODO: Set file creation time to original recording time - this didn't work
                                #win32file.SetFileTime(
                                #    downloaded_file_path,
                                #    pywintypes.Time(datetime.strptime(recent_end_time, date_format)),
                                #    None,
                                #    None,
                                #    win32con.FILE_FLAG_BACKUP_SEMANTICS
                                #)
                                print(f"File downloaded successfully as {downloaded_filename}")
                    # Process all files
                    process_all_files(updated_download_directories)
        else:
            print(f"Unable to find device")

async def check_device_availability():
    auth = httpx.DigestAuth(AUTH_USERNAME, AUTH_PASSWORD)
    async with httpx.AsyncClient() as client:
        while True:
            try:
                device_check_url = f"http://{DEVICE_IP}/cgi-bin/recordManager.cgi?action=getCaps"
                print(f"Checking for camera devices available on network: {device_check_url}")
                response = await client.get(device_check_url, auth=auth)
                if response.status_code == 200:
                    await find_files_to_download(auth)
                else:
                    print(f"Received response from camera, but query was not successful.  Status Code: {response.status_code}")
            except Exception as e:
                print(f"device was not found at {DEVICE_IP}: {e}")

            # Look for videos that were previously downloaded and combined, and just need more processing
            for group_dir in os.listdir(VIDEO_STORAGE_PATH):
                group_full_path = os.path.join(VIDEO_STORAGE_PATH, group_dir)
                if not os.path.isdir(group_full_path):
                    # Skip attempting to process anything that's not a directory
                    continue
                combined_filename = os.path.join(group_full_path, "combined.mp4")
                process_complete_file = os.path.join(group_full_path, FINISHED_FILE)
                if os.path.isfile(process_complete_file):
                    print(f"Processing already complete on {process_complete_file}")
                    continue
                match_info_file = os.path.join(group_full_path, MATCH_INFO_FILE)
                if os.path.isfile(match_info_file):
                    match_info_config = configparser.ConfigParser()
                    match_info_config.read(match_info_file)
                    match_info = match_info_config["MATCH"]
                    if match_info['start_time_offset'] and match_info['my_team_name'] and match_info['opponent_team_name'] and match_info['location']:
                        # We have all the info we need to trim and rename the file
                        trim_file_to_offset(combined_filename, match_info)
                        with open(process_complete_file, 'w') as f:
                            f.write(f"Process completed - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        continue
                    else:
                        print(f"Fill in the match_info.ini file in {group_full_path} to finish processing")
                        continue
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    print("Starting checks...")
    asyncio.run(check_device_availability())
