# Troubleshooting Guide

A practical guide for resolving common issues with the soccer-cam automated video recording pipeline.


## Camera Not Found

If the app reports that it cannot connect to the camera:

- **Is the camera powered on?** Check that the power indicator light is solid (not blinking or off).
- **Is the Ethernet cable connected?** Make sure the cable is firmly seated at both the camera and the router/switch. Try unplugging and re-plugging.
- **Is the IP address correct?** Open `config.ini` and verify the camera IP under the `[CAMERA]` section matches the camera's actual address.
- The app polls the camera every 60 seconds and logs each connection attempt. If the camera is temporarily unreachable, it will reconnect automatically once the issue is resolved.
- Connection history is tracked in `camera_state.json`, which can help pinpoint when connectivity was lost.
- **If using a WiFi router at the field:** Make sure your phone or laptop is on the same network as the camera. Many portable routers create a separate subnet that may not be reachable from your home network.


## Downloads Stuck or Failing

The pipeline is designed to handle download failures gracefully:

- **Automatic retries:** Failed downloads are retried up to 3 times before being marked as failed.
- **Crash recovery:** If the app was interrupted mid-download, it recovers on restart. The StateAuditor scans all directories and re-queues any incomplete downloads automatically.
- **Check the logs** for messages containing "DOWNLOAD: An error occurred" to understand the specific failure.
- **WiFi or network drops:** Downloads resume from scratch per-file (partial files are cleaned up automatically). This means a dropped connection wastes the progress on the current file, but does not corrupt anything.
- **If a file repeatedly fails:** Check the camera's storage for signs of corruption. Try power-cycling the camera and re-running the app. In rare cases, a file on the camera's SD card may be unreadable.


## Disk Full

The app monitors available disk space to prevent running out of storage mid-download:

- Before each download, the app checks that at least 2 GB of free space is available (by default).
- You can adjust this threshold in `config.ini`:
  ```ini
  [STORAGE]
  min_free_gb = 2
  ```
- Each game recording is typically 5-15 GB depending on duration and camera resolution.
- **To free up space:**
  - Delete old processed videos that have already been uploaded.
  - Empty your recycle bin (deleted files still consume disk space until the bin is emptied).
  - Move completed recordings to an external drive.
  - Increase the `min_free_gb` threshold if you want a larger safety margin.


## YouTube Upload Fails

Several things can cause upload failures:

- **Quota exceeded:** YouTube enforces a daily upload quota. The app detects this and automatically waits until midnight Pacific Time when the quota resets, then retries. No action is needed on your part.
- **Token expired:** If your YouTube authentication has expired, re-authenticate through the tray app's YouTube settings. The app sends an NTFY notification when this happens so you know to take action.
- **Missing credentials:** Make sure `client_secret.json` is present in your `youtube/` directory. This file is required for YouTube API access.
- **Upload interrupted:** If an upload is cut short (network drop, app restart, etc.), the file will be retried up to 3 times automatically.


## Video Processing Seems Slow

Video processing involves two main steps, both of which are generally fast:

- **Combining videos:** This stream-copies the audio and video tracks (no re-encoding), so it usually takes just a few minutes per hour of footage.
- **Trimming:** Also uses stream-copy, making it very fast -- typically just seconds.
- The processing timeout scales with file size (30 seconds per MB, with a minimum of 30 minutes), so very large files will not be prematurely terminated.
- Check the logs for "Combine" or "Trim" progress messages to confirm processing is actually running.
- **Very large files** (2+ hours of 4K panoramic video): processing can take 10-30 minutes. This is normal.
- If processing appears completely stalled, check that FFmpeg is installed and accessible. The app requires FFmpeg for all video operations.


## NTFY Notifications Not Arriving

The app uses NTFY to send push notifications for game start/end confirmation and status updates:

- **Is the NTFY app installed?** Install it from your phone's app store if you have not already.
- **Are you subscribed to the correct topic?** The topic name must match exactly between your `config.ini` (under `[NTFY]`) and the NTFY app on your phone. Topic names are case-sensitive.
- The app waits indefinitely for your responses -- there is no timeout, so take your time.
- **If you responded but nothing happened:** Check the app logs for "NTFY: Response received" messages. If the response was received but not acted on, there may be a downstream issue.
- **Self-hosted NTFY server:** Verify that the server URL in `config.ini` is correct and that the server is accessible from both your phone and the machine running the app.


## Application Won't Start

Common startup issues and their fixes:

- **"Configuration file not found":** Create `config.ini` by copying the `config.ini.dist` template and filling in your values.
- **Check `config.ini` for common mistakes:**
  - Timezone must use an underscore, not a space: `America/New_York` (not `America/New York`).
  - The storage path must point to a directory that either exists or can be created by the app.
  - The camera IP must be a valid, reachable address.
- The app validates the storage path on startup and logs a clear error if it cannot write to the configured location.
- If the app closes immediately with no visible error, try running it from a terminal (`uv run python run.py`) to see the full error output.


## Crash Recovery / Power Outage

The pipeline is designed to recover from unexpected shutdowns automatically:

- **State persistence:** Each video group's processing state is saved to a `state.json` file in its directory. This means the app always knows where it left off.
- **On restart, the StateAuditor scans all directories** and re-queues any interrupted work:
  - Downloads in "pending", "downloading", or "download_failed" state are re-queued for download.
  - Directories where all files have been downloaded but not yet combined are sent to the combine step.
  - Combined videos that are still awaiting match info are re-processed through the remaining steps.
- **Queue state is persisted to disk** between restarts, including queued items and retry counts. This means the app picks up right where it left off without losing track of retry attempts.
- In most cases, simply restarting the app after a crash or power outage is all that is needed. The pipeline will resume processing within 60 seconds.


## Videos From Wrong Time / Home Recordings

If the app is processing recordings that were made at home rather than at the field:

- The app uses "connected timeframes" to determine which recordings to process. It filters based on when the camera was connected and available.
- **If the camera is always connected to your home network**, all recordings from the camera will be processed, including those made at home. This is the most common cause of unwanted recordings.
- **Recommended workflow to avoid this:**
  1. Bring the camera to the game and record as usual.
  2. When you return home, plug the camera into your network.
  3. Let the app download and process the game recordings.
  4. Once downloads are complete, unplug the camera until the next game.
- The app can send an NTFY notification when downloads are complete, letting you know it is safe to disconnect the camera.
- This approach ensures only field recordings are captured during the "connected" window.
