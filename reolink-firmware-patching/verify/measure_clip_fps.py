#!/usr/bin/env python3
"""Measure the REAL frame rate of a camera recording.

The container header is not evidence. The Duo 3 writes the *requested* rate
into the MP4 regardless of what the encoder actually delivered, so a clip whose
header says 25 fps can easily hold 21 fps of pictures. This decodes the stream,
counts frames, and divides by the presentation-timestamp span:

    measured_fps = (frames - 1) / (last_pts - first_pts)

using (frames - 1) because N frames span N-1 inter-frame intervals. The
container's nb_frames / r_frame_rate / avg_frame_rate are reported alongside so
a disagreement between claimed and delivered is visible rather than hidden.

Usage:
    python verify/measure_clip_fps.py <clip.mp4> [<clip.mp4> ...]
    python verify/measure_clip_fps.py --json <clip.mp4>
"""

import json
import subprocess
import sys


def probe(path):
    stream = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=nb_frames,nb_read_frames,r_frame_rate,avg_frame_rate,"
                "width,height,codec_name,duration,time_base",
                "-show_entries",
                "format=duration,bit_rate",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    st = stream["streams"][0]
    fmt = stream.get("format", {})

    # Packet PTS span -- the authoritative duration of the picture sequence.
    pk = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    pts = sorted(float(x) for x in pk if x and x != "N/A")

    frames = int(st.get("nb_read_frames") or 0) or len(pts)
    span = (pts[-1] - pts[0]) if len(pts) > 1 else 0.0
    measured = (len(pts) - 1) / span if span > 0 else 0.0

    def rat(v):
        try:
            n, d = v.split("/")
            return int(n) / int(d) if int(d) else 0.0
        except Exception:
            return 0.0

    return {
        "file": path.replace("\\", "/").rsplit("/", 1)[-1],
        "codec": st.get("codec_name"),
        "size": f"{st.get('width')}x{st.get('height')}",
        "decoded_frames": frames,
        "packets": len(pts),
        "pts_span_s": round(span, 4),
        "container_duration_s": round(
            float(fmt.get("duration") or st.get("duration") or 0), 4
        ),
        "container_r_frame_rate": st.get("r_frame_rate"),
        "container_avg_frame_rate": round(rat(st.get("avg_frame_rate", "0/1")), 4),
        "measured_fps": round(measured, 4),
        "bit_rate_kbps": round(int(fmt.get("bit_rate") or 0) / 1000),
    }


def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    rows = [probe(p) for p in args]
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=2))
        return
    hdr = (
        "file",
        "size",
        "codec",
        "frames",
        "pts_span_s",
        "measured_fps",
        "hdr_r_fps",
        "hdr_avg_fps",
        "kbps",
    )
    print(
        f"{hdr[0]:<26} {hdr[1]:>10} {hdr[2]:>5} {hdr[3]:>7} {hdr[4]:>11} "
        f"{hdr[5]:>13} {hdr[6]:>10} {hdr[7]:>12} {hdr[8]:>7}"
    )
    for r in rows:
        print(
            f"{r['file']:<26} {r['size']:>10} {r['codec']:>5} "
            f"{r['decoded_frames']:>7} {r['pts_span_s']:>11.3f} "
            f"{r['measured_fps']:>13.4f} {r['container_r_frame_rate']:>10} "
            f"{r['container_avg_frame_rate']:>12.3f} {r['bit_rate_kbps']:>7}"
        )


if __name__ == "__main__":
    main()
