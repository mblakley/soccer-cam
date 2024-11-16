# Soccer Cam
Created for the purpose of automating the download and processing of videos.  Initially used for generating full recordings of youth soccer games, but can be used for any videos recorded on compatible Dahua/EmpireTech cameras.

## Compatible Cameras
- EmpireTech IPC-Color4K-B180 (verified)
- EmpireTech IPC-Color4K-T180

## Recommended Camera Setup
When configuring the camera, you'll want to configure it to use a static IP address.  This will make it easier to maintain a configuration for the camera IP address in the config.ini file.

## Runtime setup

### Install ffmpeg

(Windows PowerShell)
```
winget install --id=Gyan.FFmpeg  -e
```

### Install python and setup a virtual environment:

(Windows PowerShell)
```
winget install -e --id Python.Python.3.11
virtualenv venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate
cd video_grouper
pip install -r .\requirements.txt
```

### Create and setup configuration for the camera and application

(Windows PowerShell)
```
cp ./video_grouper/config.ini.dist ./video_grouper/config.ini
```
Open `config.ini` in your favorite text editor and fill in the configuration values that match your specific environment.

## Running the Application

To start the execution of the script, run it directly with python:

```
python .\video_grouper\video_grouper.py
```
The application will start to run, and will poll for the presence of the camera at the configured IP address.  Once it finds a camera that can be queried and returns a success response, it will find any videos that have been recorded to the SD card and start to download them to the configured video storage location.  The recording files are grouped into directories by date and time, and once all recordings have been downloaded, all recordings in each directory are combined into a single video.

## Running in Docker

To start the application in Docker:

```
docker compose build
docker compose up -d
```

The `-d` keeps it running in the background, so it will continue to look for new videos and process them

## Adding team information and trimming to a start time

After the videos have been downloaded and combined, it's possible to add more information to the video and trim the "warm up" time off the beginning of the video file by filling in the match_info.ini in the video directory.  You can find an example file in ./video_grouper/match_info.ini.dist

The values to fill in are:
`start_time_offset` - The time to trim off the start of the video in the format "<minutes>:<seconds>" (ex: "01:15").
`my_team_name` - The name of your team.  Any spaces will be removed from this value when creating filenames and directories.
`opponent_team_name` - The name of the opposing team.  Any spaces will be removed from this value when creating filenames and directories.
`location` - normally "home" or "away", but could be a specific field or other identifier

An unconfigured `match_info.ini` file will be created in the directory where it needs to be filled in.  All fields are required.  After you provide the required information, the next run of the device check will trim and rename the existing `combined.mp4` to something more specific, and copy it into a specifically named directory.

Once the team info has been added, processing is complete, and a new `complete.txt` file will be created in the directory with information about when the processing actually finished.  This file will be used to determine whether to skip any additional processing.


# (Optional) Notes on setting up the camera on a tripod
It's possible to setup one (or more) security cameras on a tripod to record a 180 degree view of sporting events.  This setup costs < $600 (as of May 2024), uses easily replaceable/commercially available components, and you will own the footage for as long as you want to store it.  It does require some effort to initially setup the camera and this application, but it generally takes much less time than it takes to watch a soccer game!

### Why not use a camcorder or automated gimbal?
Recording using a DSLR, cell phone, or camcorder is great for catching highlights of a single player, but it usually only captures the action around the ball.  If you are trying to figure out what the *team* is doing, especially away from the ball, it's almost impossible to capture a full picture with a single camera.  Running a camera like this also requires a lot of manual interaction (moving the camera, refocusing), and I wanted to be able to enjoy the game with my own eyes instead of watching it through a screen!

### What about commercial solutions?
Veo, Trace, Hudl, Reeplayer and other commercial solutions are great because they do a lot of this work for you, and they have put a lot of effort into improving their video quality.  If you have the money to put toward one of those solutions, and you don't care about preserving full game videos beyond the life of your subscription, you'll probably be happier using one of them.

Many of the existing commercial solutions let you tag and clip parts of the video through their web portal, but most don't let you download the raw game footage, or they require a certain subscription level before they will allow you to download it.

### What are the limitations of this setup
- Frame Rate - the FPS (frames per second) of the recording is limited by the camera hardware, and commonly only goes up to ~25fps.
- Image resolution - The width of the image in pixels is comparable to what would be generated from a commercial solution, but the number of pixels in the height is smaller, since the aspect ratio of the cameras is rectangular instead of square.  This makes image quality in the far corners of the field worse than the closer areas of the field, especially when applying any video processing.
- The camera doesn't "follow the ball", it's a static 180 degree "warped" image.  This can potentially be done as a post-processing step (see [Potential Improvements](#potential-improvements)).
- Getting a realtime view of the video is possible, but requires additional hardware and setup of a wireless router, as well as setup of a phone or tablet with ethernet connectivity.
- Streaming the video is also possible through a cell phone or wired internet connection, but it requires additional setup and data.
- Audio is recorded, but it's not high quality.

### What parts do I need?
- A 180 degree security camera (< $300)
- A 16' tripod ($100 - $150)
- A 128GB+ microSDXC memory card (~$20)
- A DC battery pack that provides 12V@5A + USB power (~$70)
- A 16+' DC extension cable with 5.5mm x 2.5mm connector (~$10)
- A Universal Pole Mounting Joint Bracket Adapter (~$10)
- A nut and washer for attaching the bracket to the tripod camera mount (< $1)
- Non-slip drawer liner (~$10)
- Some metal pipe strap (~$5)
- An ethernet cable (~$5)
- (optional) A USB powered wireless router (~$40)
- (Optional) 1/2 inch braided cable sleeve (~$10)
- A Windows, Mac, or Linux PC that has a few GBs of storage available - enough to store and process the video files

### Step-by-Step Setup
#### Physical Setup
1. Insert the microSDXC memory card into the security camera.
2. Attach the wedge adapter to the top of the tripod using the washer and nut to secure it.
3. Attach the metal pipe strap to one side of the wedge adapter.
4. Place the non-slip drawer liner on top of the wedge adapter and hold it in place.
5. Place the security camera on top of the non-slip liner (right-side up) and attach the top screw to the metal pipe strap.
6. Attach the metal pipe strap to the other side of the wedge adapter.  You may need to add multiple layers of the drawer liner to add enough tension to keep the camera in place.
7. Adjust the wedge adapter to point the camera down toward the ground.
8. Connect the DC extension cable between the DC battery pack and the camera.

#### Physical Setup (for image preview)
1. Connect the ethernet cable between the wireless router and the camera.
2. Connect the USB power cable between the wireless router and the DC battery pack.

#### Software Configuration
1. On the camera:
    - Configure recording to automatically start whenever the camera is powered on.
    - Configure the camera to record with the highest bit-rate and FPS possible.
    - Turn off any "AI" functionality to detect objects, since this will decrease frame rate.
2. On the router:
    - Configure the IP address of the camera to be static.  This will be the IP address that you will connect to for image preview on your phone, and to download videos from.

#### Starting a Recording
1. Set up the tripod lined up with center field, around 15 - 20ft outside of the touch line.  Extend the tripod to the maximum height.
2. Power on the DC battery pack.
3. (optional - for image preview) Once the wireless router starts advertising its SSID, connect to it with your cell phone or tablet.
4. (optional - for image preview) Open the browser on your cell phone or tablet, and connect to the static IP address of the camera.  Note: You may need to turn off cell data to allow this to work correctly.
5. (optional - for image preview) Login to the camera web UI using your configured credentials and navigate to the image preview in the camera web UI.
6. Adjust the camera positioning to capture the full field.  You may need to move the camera toward or away from the field, or rotate the tripod to capture both near corners of the field.
7. When the game is over, power off the DC battery pack, which will stop the recording.

#### Recommended Configuration and Workflow After Games
- Configure the wireless router to use the same IP range as your home internet.  This will allow a workflow of coming home from a game, connecting the camera to your home network via an ethernet cable, plugging the DC power adapter in to recharge and turning it on.
- Run the script on your laptop that's connected to the same network that the camera is connected to.  The camera will power on and connect to the network, and the running script will connect to the camera as soon as it's available.

### Editing the video
You can use any video editing tools you want to cut clips out of the full video.  I tend to just use `ffmpeg` on the command line to cut the video closer to the start of the game.  Use a video player to find the time of the kickoff, and then input the time offset into this command line:
```
ffmpeg -ss 00:02:45.0 -i .\original.mp4 -c copy .\clipped.mp4
```

If you follow the instructions above and create the `match_info.ini` file after the video has been created, the app will use the information provided to trim the video file for you.

### My video processing tool is complaining about the format of the video file
The video that comes off the camera isn't always consistent with how the frames are written.  To repair the video, you can run this command, at a small (imperceptible) cost to image and audio quality.
```
ffmpeg -i C:\Users\myuser\Downloads\original-video.mp4 -c:v libx264 -crf 23 -c:a aac -strict -2 C:\Users\myuser\Downloads\fixed-video.mp4
```

### Where to put the video
Once you have combined the video files for a game, you can upload it to a video hosting site, keep it on local storage, or upload it to your cloud storage provider of choice.  The files are *large* so be aware of any size limitations wherever you want to store videos.

Using YouTube for the ability to link timestamps and create short clips seems to be a fairly simple way to analyze and share game video.

### Can I manually download the videos from the camera?
Sure!  It takes a long time due to the slow ethernet connection, which eventually results in an authentication timeout through the camera's web UI, so you may need to re-download some of the videos if that happens before the downloads have all completed.

You can manually combine the videos using `ffmpeg`, if you put all the filenames in an ordered list and save it as a text file.
```
Get-ChildItem -Path . -Filter *.mp4 | Sort-Object Name | ForEach-Object { "file '$($_.Name)'" } | Out-File -FilePath "output.txt" -Encoding ASCII
ffmpeg -f concat -safe 0 -i video_list.txt -c copy output.mp4
```

### Modifying the camera for weight saving
The camera itself is pretty heavy, and putting it at the top of a tall pole means it will move in the wind.  It's possible to remove the heavy "base" part of the camera to reduce the overall weight, but it requires modifying the ethernet cable so it can fit through the hole in the base.

I cut the ethernet cable off close to the female connector, crimped a new male ethernet onto the cable (configured to 10/100 specs, since there are only 4 wires), and purchased a male-to-male ethernet adapter from my local tech store.  This allowed me to pull the cable through the base after cutting it, unbolt a few pieces, and then put everything back in working order!

## Potential Improvements
- Video post-processing.  It should be able "follow the ball", either by manually inputting X coordinates and timestamps, or by running a ball detection algorithm to generate the coordinates, and sliding the frame back and forth across the image.
- Audio quality.  It's possible to connect a microphone to the camera, but I haven't tried it.
- Setup HTTPS security.  It should be possible to generate a certificate to allow the camera to serve the Web UI over HTTPS.  This isn't a huge security risk, since I'm usually recording in the middle of a field.
