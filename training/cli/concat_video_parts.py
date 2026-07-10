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

    def _stream_from_template(out_c, vs):
        """PyAV-version-tolerant template stream: add_stream_from_template (>=12),
        add_stream(template=) (10-11), else manual codec-context copy (older)."""
        if hasattr(out_c, "add_stream_from_template"):
            return out_c.add_stream_from_template(vs)
        try:
            return out_c.add_stream(template=vs)
        except TypeError:
            cc = vs.codec_context
            st = out_c.add_stream(cc.name, rate=vs.average_rate)
            st.width, st.height, st.pix_fmt = cc.width, cc.height, cc.pix_fmt
            st.codec_context.time_base = vs.time_base
            if cc.extradata:  # SPS/PPS — required for an h264 packet remux
                st.codec_context.extradata = cc.extradata
            return st

    with av.open(args.out, mode="w") as out_c:
        out_stream = None
        offset = 0
        for part in args.parts:
            with av.open(part) as in_c:
                vs = in_c.streams.video[0]
                if out_stream is None:
                    out_stream = _stream_from_template(out_c, vs)
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
