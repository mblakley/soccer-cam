"""Batch process videos: extract frames -> tile -> cleanup.

Processes a list of (video_path, game_id) pairs sequentially.
Prints structured progress updates.
"""

import argparse
import logging
import time
from pathlib import Path

from training.data_prep.extract_frames import extract_frames
from training.data_prep.tile_frames import tile_frame

logger = logging.getLogger(__name__)

# fmt: off
# 15 complete games, 119 segments total (all segments per game for full coverage)
VIDEOS = [
    # === HEAT: Heat Tournament (17 segments, 3 sub-games) ===
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 1/17.36.31-17.53.37[F][0@0][220902]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 1/17.53.37-18.10.24[F][0@0][221897]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 1/18.10.24-18.27.10[F][0@0][222892]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 1/18.27.11-18.43.56[F][0@0][223887]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 1/18.43.56-19.00.50[F][0@0][224882]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 1/19.00.50-19.08.10[F][0@0][225877]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 2/07.45.31-08.02.21[F][0@0][226331]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 2/08.02.21-08.19.12[F][0@0][227326]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 2/08.19.12-08.36.06[F][0@0][228321]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 2/08.36.06-08.52.53[F][0@0][229316]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 2/08.52.53-09.07.51[F][0@0][230311]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 3/08.35.21-08.52.16[F][0@0][231212]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 3/08.52.16-09.09.05[F][0@0][232207]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 3/09.09.05-09.25.54[F][0@0][233203]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 3/09.25.54-09.42.45[F][0@0][234198]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 3/09.42.45-09.59.40[F][0@0][235193]_ch1.mp4", "heat__Heat_Tournament"),
    ("F:/Heat_2012s/06.07.2024-06.09.2024 - Heat Tournament/Game 3/09.59.40-10.03.13[F][0@0][236188]_ch1.mp4", "heat__Heat_Tournament"),
    # === HEAT: Clarence Tournament (15 segments, 3 sub-games) ===
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 1/09.43.45-10.00.32[F][0@0][144123].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 1/10.00.32-10.17.18[F][0@0][145119].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 1/10.17.18-10.34.03[F][0@0][146114].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 1/10.34.03-10.50.56[F][0@0][147109].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 1/10.50.56-10.54.04[F][0@0][148104].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 2/13.27.57-13.44.49[F][0@0][148305].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 2/13.44.50-14.01.35[F][0@0][149300].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 2/14.01.35-14.18.21[F][0@0][150295].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 2/14.18.21-14.35.12[F][0@0][151290].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 2/14.35.12-14.37.04[F][0@0][152285].mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 3/12.14.12-12.31.00[F][0@0][159041]_ch1.mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 3/12.30.59-12.47.44[F][0@0][160036]_ch1.mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 3/12.47.44-13.04.31[F][0@0][161031]_ch1.mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 3/13.04.31-13.21.18[F][0@0][162027]_ch1.mp4", "heat__Clarence_Tournament"),
    ("F:/Heat_2012s/07.20.2024-07.21.2024 - Clarence Tournament/Game 3/13.21.18-13.22.40[F][0@0][163022]_ch1.mp4", "heat__Clarence_Tournament"),
    # === HEAT: vs Chili away (7 segments) ===
    ("F:/Heat_2012s/06.20.2024 - vs Chili (away)/17.59.23-18.16.09[F][0@0][17375]_ch1.mp4", "heat__06.20.2024_vs_Chili_away"),
    ("F:/Heat_2012s/06.20.2024 - vs Chili (away)/18.16.09-18.33.04[F][0@0][18370]_ch1.mp4", "heat__06.20.2024_vs_Chili_away"),
    ("F:/Heat_2012s/06.20.2024 - vs Chili (away)/18.33.04-18.49.52[F][0@0][19366]_ch1.mp4", "heat__06.20.2024_vs_Chili_away"),
    ("F:/Heat_2012s/06.20.2024 - vs Chili (away)/18.49.52-19.06.42[F][0@0][20361]_ch1.mp4", "heat__06.20.2024_vs_Chili_away"),
    ("F:/Heat_2012s/06.20.2024 - vs Chili (away)/19.06.42-19.23.32[F][0@0][21356]_ch1.mp4", "heat__06.20.2024_vs_Chili_away"),
    ("F:/Heat_2012s/06.20.2024 - vs Chili (away)/19.23.32-19.40.18[F][0@0][22351]_ch1.mp4", "heat__06.20.2024_vs_Chili_away"),
    ("F:/Heat_2012s/06.20.2024 - vs Chili (away)/19.40.18-19.47.04[F][0@0][23346]_ch1.mp4", "heat__06.20.2024_vs_Chili_away"),
    # === HEAT: vs Fairport home (6 segments) ===
    ("F:/Heat_2012s/05.31.2024 - vs Fairport (home)/18.01.30-18.18.20[F][0@0][189242]_ch1.mp4", "heat__05.31.2024_vs_Fairport_home"),
    ("F:/Heat_2012s/05.31.2024 - vs Fairport (home)/18.18.20-18.35.11[F][0@0][190237]_ch1.mp4", "heat__05.31.2024_vs_Fairport_home"),
    ("F:/Heat_2012s/05.31.2024 - vs Fairport (home)/18.35.11-18.52.02[F][0@0][191232]_ch1.mp4", "heat__05.31.2024_vs_Fairport_home"),
    ("F:/Heat_2012s/05.31.2024 - vs Fairport (home)/18.52.02-19.08.47[F][0@0][192227]_ch1.mp4", "heat__05.31.2024_vs_Fairport_home"),
    ("F:/Heat_2012s/05.31.2024 - vs Fairport (home)/19.08.47-19.25.36[F][0@0][193222]_ch1.mp4", "heat__05.31.2024_vs_Fairport_home"),
    ("F:/Heat_2012s/05.31.2024 - vs Fairport (home)/19.25.36-19.40.33[F][0@0][194217]_ch1.mp4", "heat__05.31.2024_vs_Fairport_home"),
    # === HEAT: vs Fairport away (6 segments) ===
    ("F:/Heat_2012s/07.17.2024 - vs Fairport (away)/18.15.29-18.32.15[F][0@0][138944].mp4", "heat__07.17.2024_vs_Fairport_away"),
    ("F:/Heat_2012s/07.17.2024 - vs Fairport (away)/18.32.15-18.49.05[F][0@0][139939].mp4", "heat__07.17.2024_vs_Fairport_away"),
    ("F:/Heat_2012s/07.17.2024 - vs Fairport (away)/18.49.05-19.05.55[F][0@0][140934].mp4", "heat__07.17.2024_vs_Fairport_away"),
    ("F:/Heat_2012s/07.17.2024 - vs Fairport (away)/19.05.55-19.22.38[F][0@0][141929].mp4", "heat__07.17.2024_vs_Fairport_away"),
    ("F:/Heat_2012s/07.17.2024 - vs Fairport (away)/19.22.38-19.39.29[F][0@0][142924].mp4", "heat__07.17.2024_vs_Fairport_away"),
    ("F:/Heat_2012s/07.17.2024 - vs Fairport (away)/19.39.29-19.42.51[F][0@0][143920].mp4", "heat__07.17.2024_vs_Fairport_away"),
    # === CAMERA: 2025.03.17 (9 segments) ===
    ("F:/Camera/2025.03.17-16.35.49/16.35.49-16.52.42[F][0@0][68723].mp4", "camera__2025.03.17"),
    ("F:/Camera/2025.03.17-16.35.49/16.52.42-17.09.43[F][0@0][69718].mp4", "camera__2025.03.17"),
    ("F:/Camera/2025.03.17-16.35.49/17.09.43-17.26.32[F][0@0][70713].mp4", "camera__2025.03.17"),
    ("F:/Camera/2025.03.17-16.35.49/17.26.32-17.43.16[F][0@0][71708].mp4", "camera__2025.03.17"),
    ("F:/Camera/2025.03.17-16.35.49/17.43.16-18.00.15[F][0@0][72703].mp4", "camera__2025.03.17"),
    ("F:/Camera/2025.03.17-16.35.49/18.00.15-18.16.53[F][0@0][73698].mp4", "camera__2025.03.17"),
    ("F:/Camera/2025.03.17-16.35.49/18.16.53-18.33.42[F][0@0][74693].mp4", "camera__2025.03.17"),
    ("F:/Camera/2025.03.17-16.35.49/18.33.43-18.50.26[F][0@0][75688].mp4", "camera__2025.03.17"),
    ("F:/Camera/2025.03.17-16.35.49/18.50.26-19.05.59[F][0@0][76683].mp4", "camera__2025.03.17"),
    # === CAMERA: 2025.03.03 (7 segments) ===
    ("F:/Camera/2025.03.03-16.39.29/16.39.29-16.56.21[F][0@0][49840].mp4", "camera__2025.03.03"),
    ("F:/Camera/2025.03.03-16.39.29/16.56.21-17.13.00[F][0@0][50836].mp4", "camera__2025.03.03"),
    ("F:/Camera/2025.03.03-16.39.29/17.13.00-17.29.47[F][0@0][51831].mp4", "camera__2025.03.03"),
    ("F:/Camera/2025.03.03-16.39.29/17.29.47-17.46.38[F][0@0][52826].mp4", "camera__2025.03.03"),
    ("F:/Camera/2025.03.03-16.39.29/17.46.39-18.03.29[F][0@0][53821].mp4", "camera__2025.03.03"),
    ("F:/Camera/2025.03.03-16.39.29/18.03.29-18.20.14[F][0@0][54816].mp4", "camera__2025.03.03"),
    ("F:/Camera/2025.03.03-16.39.29/18.20.14-18.22.25[F][0@0][55811].mp4", "camera__2025.03.03"),
    # === CAMERA: 2025.03.24 (7 segments) ===
    ("F:/Camera/2025.03.24-16.41.18/16.41.18-16.58.04[F][0@0][80013].mp4", "camera__2025.03.24"),
    ("F:/Camera/2025.03.24-16.41.18/16.58.04-17.14.57[F][0@0][81009].mp4", "camera__2025.03.24"),
    ("F:/Camera/2025.03.24-16.41.18/17.14.57-17.31.57[F][0@0][82004].mp4", "camera__2025.03.24"),
    ("F:/Camera/2025.03.24-16.41.18/17.31.57-17.48.58[F][0@0][82999].mp4", "camera__2025.03.24"),
    ("F:/Camera/2025.03.24-16.41.18/17.48.58-18.05.52[F][0@0][83994].mp4", "camera__2025.03.24"),
    ("F:/Camera/2025.03.24-16.41.18/18.05.52-18.22.48[F][0@0][84989].mp4", "camera__2025.03.24"),
    ("F:/Camera/2025.03.24-16.41.18/18.22.48-18.29.52[F][0@0][85984].mp4", "camera__2025.03.24"),
    # === CAMERA: 2025.03.31 (7 segments) ===
    ("F:/Camera/2025.03.31-17.25.16/17.25.16-17.42.05[F][0@0][95645].mp4", "camera__2025.03.31"),
    ("F:/Camera/2025.03.31-17.25.16/17.42.05-17.58.54[F][0@0][96640].mp4", "camera__2025.03.31"),
    ("F:/Camera/2025.03.31-17.25.16/17.58.54-18.15.44[F][0@0][97635].mp4", "camera__2025.03.31"),
    ("F:/Camera/2025.03.31-17.25.16/18.15.44-18.32.43[F][0@0][98630].mp4", "camera__2025.03.31"),
    ("F:/Camera/2025.03.31-17.25.16/18.32.43-18.49.43[F][0@0][99625].mp4", "camera__2025.03.31"),
    ("F:/Camera/2025.03.31-17.25.16/18.49.43-19.06.39[F][0@0][100620].mp4", "camera__2025.03.31"),
    ("F:/Camera/2025.03.31-17.25.16/19.06.39-19.07.22[F][0@0][101615].mp4", "camera__2025.03.31"),
    # === CAMERA: 2025.03.10 (6 segments) ===
    ("F:/Camera/2025.03.10-17.29.36/17.29.36-17.46.22[F][0@0][59510].mp4", "camera__2025.03.10"),
    ("F:/Camera/2025.03.10-17.29.36/17.46.22-18.03.00[F][0@0][60506].mp4", "camera__2025.03.10"),
    ("F:/Camera/2025.03.10-17.29.36/18.03.01-18.19.48[F][0@0][61501].mp4", "camera__2025.03.10"),
    ("F:/Camera/2025.03.10-17.29.36/18.19.48-18.36.33[F][0@0][62496].mp4", "camera__2025.03.10"),
    ("F:/Camera/2025.03.10-17.29.36/18.36.33-18.53.28[F][0@0][63491].mp4", "camera__2025.03.10"),
    ("F:/Camera/2025.03.10-17.29.36/18.53.28-19.08.05[F][0@0][64486].mp4", "camera__2025.03.10"),
    # === CAMERA: 2025.04.07 (6 segments) ===
    ("F:/Camera/2025.04.07-17.26.53/17.26.53-17.43.36[F][0@0][103281].mp4", "camera__2025.04.07"),
    ("F:/Camera/2025.04.07-17.26.53/17.43.36-18.00.11[F][0@0][104277].mp4", "camera__2025.04.07"),
    ("F:/Camera/2025.04.07-17.26.53/18.00.11-18.17.08[F][0@0][105272].mp4", "camera__2025.04.07"),
    ("F:/Camera/2025.04.07-17.26.53/18.17.08-18.33.57[F][0@0][106268].mp4", "camera__2025.04.07"),
    ("F:/Camera/2025.04.07-17.26.53/18.33.57-18.50.51[F][0@0][107264].mp4", "camera__2025.04.07"),
    ("F:/Camera/2025.04.07-17.26.53/18.50.51-19.05.55[F][0@0][108259].mp4", "camera__2025.04.07"),
    # === FLASH: vs Chili home (7 segments) ===
    ("F:/Flash_2013s/09.30.2024 - vs Chili (home)/17.12.13-17.29.02[F][0@0][233873].mp4", "flash__09.30.2024_vs_Chili_home"),
    ("F:/Flash_2013s/09.30.2024 - vs Chili (home)/17.29.02-17.46.00[F][0@0][234868].mp4", "flash__09.30.2024_vs_Chili_home"),
    ("F:/Flash_2013s/09.30.2024 - vs Chili (home)/17.46.00-18.02.52[F][0@0][235863].mp4", "flash__09.30.2024_vs_Chili_home"),
    ("F:/Flash_2013s/09.30.2024 - vs Chili (home)/18.02.52-18.19.36[F][0@0][236858].mp4", "flash__09.30.2024_vs_Chili_home"),
    ("F:/Flash_2013s/09.30.2024 - vs Chili (home)/18.19.36-18.36.17[F][0@0][237854].mp4", "flash__09.30.2024_vs_Chili_home"),
    ("F:/Flash_2013s/09.30.2024 - vs Chili (home)/18.36.17-18.53.16[F][0@0][238849].mp4", "flash__09.30.2024_vs_Chili_home"),
    ("F:/Flash_2013s/09.30.2024 - vs Chili (home)/18.53.16-18.54.39[F][0@0][239845].mp4", "flash__09.30.2024_vs_Chili_home"),
    # === FLASH: vs RNYFC Black home (7 segments) ===
    ("F:/Flash_2013s/09.27.2024 - vs RNYFC Black (home)/10.19.41-10.20.39[F][0@0][183725]_ch1.mp4", "flash__09.27.2024_vs_RNYFC_Black_home"),
    ("F:/Flash_2013s/09.27.2024 - vs RNYFC Black (home)/20.09.35-20.26.21[F][0@0][178488]_ch1.mp4", "flash__09.27.2024_vs_RNYFC_Black_home"),
    ("F:/Flash_2013s/09.27.2024 - vs RNYFC Black (home)/20.26.21-20.43.00[F][0@0][179483]_ch1.mp4", "flash__09.27.2024_vs_RNYFC_Black_home"),
    ("F:/Flash_2013s/09.27.2024 - vs RNYFC Black (home)/20.43.00-20.59.52[F][0@0][180479]_ch1.mp4", "flash__09.27.2024_vs_RNYFC_Black_home"),
    ("F:/Flash_2013s/09.27.2024 - vs RNYFC Black (home)/20.59.52-21.16.42[F][0@0][181475]_ch1.mp4", "flash__09.27.2024_vs_RNYFC_Black_home"),
    ("F:/Flash_2013s/09.27.2024 - vs RNYFC Black (home)/21.16.42-21.33.31[F][0@0][182470]_ch1.mp4", "flash__09.27.2024_vs_RNYFC_Black_home"),
    ("F:/Flash_2013s/09.27.2024 - vs RNYFC Black (home)/21.33.32-21.37.34[F][0@0][183465]_ch1.mp4", "flash__09.27.2024_vs_RNYFC_Black_home"),
    # === FLASH: vs IYSA home (6 segments) ===
    ("F:/Flash_2013s/06.01.2024 - vs IYSA (home)/16.37.17-16.54.08[F][0@0][195104]_ch1.mp4", "flash__06.01.2024_vs_IYSA_home"),
    ("F:/Flash_2013s/06.01.2024 - vs IYSA (home)/16.54.08-17.11.00[F][0@0][196100]_ch1.mp4", "flash__06.01.2024_vs_IYSA_home"),
    ("F:/Flash_2013s/06.01.2024 - vs IYSA (home)/17.10.59-17.27.41[F][0@0][197095]_ch1.mp4", "flash__06.01.2024_vs_IYSA_home"),
    ("F:/Flash_2013s/06.01.2024 - vs IYSA (home)/17.27.41-17.44.31[F][0@0][198090]_ch1.mp4", "flash__06.01.2024_vs_IYSA_home"),
    ("F:/Flash_2013s/06.01.2024 - vs IYSA (home)/17.44.32-18.01.21[F][0@0][199085]_ch1.mp4", "flash__06.01.2024_vs_IYSA_home"),
    ("F:/Flash_2013s/06.01.2024 - vs IYSA (home)/18.01.21-18.11.43[F][0@0][200081]_ch1.mp4", "flash__06.01.2024_vs_IYSA_home"),
    # === FLASH: 2025.06.02 (6 segments) ===
    ("F:/Flash_2013s/2025.06.02-18.16.03/18.16.03-18.32.49[F][0@0][190830].mp4", "flash__2025.06.02"),
    ("F:/Flash_2013s/2025.06.02-18.16.03/18.32.49-18.49.37[F][0@0][191825].mp4", "flash__2025.06.02"),
    ("F:/Flash_2013s/2025.06.02-18.16.03/18.49.37-19.06.27[F][0@0][192820].mp4", "flash__2025.06.02"),
    ("F:/Flash_2013s/2025.06.02-18.16.03/19.06.27-19.23.13[F][0@0][193815].mp4", "flash__2025.06.02"),
    ("F:/Flash_2013s/2025.06.02-18.16.03/19.23.13-19.40.02[F][0@0][194810].mp4", "flash__2025.06.02"),
    ("F:/Flash_2013s/2025.06.02-18.16.03/19.40.02-19.48.24[F][0@0][195805].mp4", "flash__2025.06.02"),
]
# fmt: on

DIFF_THRESHOLD = 2.0  # Lowered from 5.0 to capture more frames from static camera
FRAME_INTERVAL = 8  # Extract every 8th frame (~3 fps at 24.6 fps source)

# Tiling grid: 7x3 produces exact 640x640 tiles from 4096x1800 panoramic frames
TILE_COLS = 7
TILE_ROWS = 3
TILE_SIZE = 640


def process_video(
    video_path: str, game_id: str, frames_base: Path, tiles_base: Path
) -> dict:
    """Process a single video segment: extract -> tile -> cleanup."""
    import shutil

    video = Path(video_path)
    segment_id = video.stem  # unique per segment within a game
    frames_dir = frames_base / game_id / segment_id
    tiles_dir = tiles_base / game_id

    # Skip if this segment's tiles already exist (check by filename prefix)
    existing = (
        list(tiles_dir.glob(f"{segment_id}_*_r0_c0.jpg")) if tiles_dir.exists() else []
    )
    if existing:
        return {"game_id": game_id, "status": "skipped", "tiles": 0}

    # Extract frames into segment-specific dir
    n_frames = extract_frames(
        video, frames_dir, diff_threshold=DIFF_THRESHOLD, frame_interval=FRAME_INTERVAL
    )

    # Tile all frames into shared game tiles dir
    frame_files = sorted(frames_dir.rglob("*.jpg"))
    n_tiles = 0
    for frame_path in frame_files:
        tiles = tile_frame(
            frame_path, tiles_dir, cols=TILE_COLS, rows=TILE_ROWS, tile_size=TILE_SIZE
        )
        n_tiles += len(tiles)

    # Cleanup raw frames for this segment
    if frames_dir.exists():
        shutil.rmtree(frames_dir)

    return {"game_id": game_id, "status": "done", "frames": n_frames, "tiles": n_tiles}


def main():
    parser = argparse.ArgumentParser(description="Batch process videos for training")
    parser.add_argument(
        "--frames-dir",
        type=Path,
        default=Path("F:/training_data/frames"),
    )
    parser.add_argument(
        "--tiles-dir",
        type=Path,
        default=Path("F:/training_data/tiles_640"),
    )
    parser.add_argument(
        "--start", type=int, default=0, help="Start index (for resuming)"
    )
    parser.add_argument("--end", type=int, default=len(VIDEOS), help="End index")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    total_videos = args.end - args.start
    total_tiles = 0
    total_frames = 0
    start_time = time.time()

    for i, (video_path, game_id) in enumerate(VIDEOS[args.start : args.end], start=1):
        logger.info("=== [%d/%d] %s ===", i, total_videos, game_id)
        try:
            result = process_video(video_path, game_id, args.frames_dir, args.tiles_dir)
            if result["status"] == "skipped":
                logger.info("SKIPPED (already has %d tiles)", result["tiles"])
                total_tiles += result["tiles"]
            else:
                total_frames += result.get("frames", 0)
                total_tiles += result.get("tiles", 0)
                elapsed = time.time() - start_time
                rate = i / elapsed * 60  # videos per minute
                logger.info(
                    "OK: %d frames -> %d tiles | Running total: %d tiles | %.1f vid/min",
                    result.get("frames", 0),
                    result.get("tiles", 0),
                    total_tiles,
                    rate,
                )
        except Exception:
            logger.exception("FAILED: %s", game_id)

    elapsed = time.time() - start_time
    logger.info(
        "=== COMPLETE: %d videos, %d frames, %d tiles in %.0f min ===",
        total_videos,
        total_frames,
        total_tiles,
        elapsed / 60,
    )


if __name__ == "__main__":
    main()
