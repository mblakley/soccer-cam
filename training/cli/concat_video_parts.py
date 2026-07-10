"""Concatenate same-codec video parts by packet remux (no re-encode).

The parallel full-game render (N workers over N frame ranges — decode is the
single-stream ceiling at ~11 fps, so time-slicing the game is the only big
multiplier on a 4-core box) produces part files with identical encoder
parameters; this stitches them losslessly by offsetting packet timestamps.

    python -m training.cli.concat_video_parts --out full.mp4 part1.mp4 part2.mp4 ...
"""

from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("parts", nargs="+")
    args = ap.parse_args()

    import av

    with av.open(args.out, mode="w") as out_c:
        out_stream = None
        offset = 0
        for part in args.parts:
            with av.open(part) as in_c:
                vs = in_c.streams.video[0]
                if out_stream is None:
                    out_stream = out_c.add_stream(template=vs)
                span = 0
                for packet in in_c.demux(vs):
                    if packet.dts is None:
                        continue
                    packet.stream = out_stream
                    packet.pts = (packet.pts or 0) + offset
                    packet.dts = packet.dts + offset
                    span = max(span, packet.pts + (packet.duration or 0))
                    out_c.mux(packet)
                offset = span
            print(f"appended {part} (next offset {offset})")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
