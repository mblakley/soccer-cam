"""Field-boundary editor backed by the canonical per-game ``game.json`` sidecars (next to each
video on F:), independent of the legacy ``manifest.db`` / ``v4_fields`` store. Lets a human review
and drag-correct the 10-point field polygon for ANY registry game (including the auto-seeded,
``field_polygon_verified=false`` ones), with the canvas background decoded straight from the video.

Mounted into the annotation server via ``app.include_router(field_edit_v2.router)``; the page lives at
``/static/field-edit.html`` and talks to ``/api/fieldv2/*``. Saving sets
``field_polygon_source='human_field_edit'`` + ``field_polygon_verified=true``.
"""

import glob
import io
import json
import os

import av
from fastapi import APIRouter, HTTPException, Request, Response
from PIL import Image

router = APIRouter()

REG = os.environ.get("GAME_REGISTRY", r"F:\training_data\game_registry.json")
DISP_W = 1280


def _games():
    with open(REG, encoding="utf-8") as f:
        return json.load(f)


def _resolve(gid):
    """Return (video_dir, game_json_path) for a game id, or (None, None)."""
    for g in _games():
        if g["game_id"] == gid:
            p = g.get("path")
            if not p:
                return None, None
            vd = os.path.dirname(p) if os.path.isfile(p) else p
            return vd, os.path.join(vd, "game.json")
    return None, None


def _seg_file(vd, seg):
    for c in (seg + ".mp4", seg + ".MP4"):
        if os.path.exists(os.path.join(vd, c)):
            return os.path.join(vd, c)
    vids = [
        h
        for h in glob.glob(os.path.join(vd, glob.escape(seg) + "*"))
        if h.lower().endswith((".mp4", ".mov", ".mkv"))
    ]
    return vids[0] if vids else None


def _any_footage(vd):
    for cand in glob.glob(os.path.join(vd, "**", "*.mp4"), recursive=True):
        b = os.path.basename(cand).lower()
        if any(k in b for k in ("[f]", "_ch", "recm09", "raw", "combined")):
            return cand
    return None


def _decode_mid(path):
    """Decode a ~mid-game frame as a PIL image via PyAV (the annotation venv has no cv2)."""
    container = av.open(path)
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        if stream.duration and stream.time_base:
            dur = float(stream.duration * stream.time_base)
        elif container.duration:
            dur = container.duration / av.time_base
        else:
            dur = 0.0
        t = dur * 0.5
        if stream.time_base and t > 0:
            container.seek(int(t / stream.time_base), stream=stream, backward=True)
        for frame in container.decode(stream):
            return frame.to_image()  # PIL RGB
        return None
    finally:
        container.close()


@router.get("/api/fieldv2/games")
def games_list():
    """All registry games that have a game.json, with polygon/verify status (for the picker)."""
    out = []
    for g in _games():
        # Raw camera captures (e.g. "camera__2025.01.16_13.34.59") are setup /
        # indoor / house footage, not games -- keep them out of the review picker.
        if g["game_id"].startswith("camera__"):
            continue
        gjp = _resolve(g["game_id"])[1]
        if not gjp or not os.path.exists(gjp):
            continue
        gj = json.load(open(gjp, encoding="utf-8"))
        # Explicitly excluded games (field_polygon_note starting "exclud", e.g.
        # indoor/house footage that isn't a game) are dropped from the picker.
        if str(gj.get("field_polygon_note", "")).lower().startswith("exclud"):
            continue
        hp = bool(gj.get("field_polygon"))
        src = str(gj.get("field_polygon_source", ""))
        out.append(
            {
                "game_id": g["game_id"],
                "trainable": bool(g.get("trainable")),
                "camera": gj.get("camera") or gj.get("video_format"),
                "has_polygon": hp,
                "source": gj.get("field_polygon_source"),
                # verified: explicit flag, else true if a human/manifest polygon exists, else false
                "verified": gj.get(
                    "field_polygon_verified", hp and not src.startswith("auto_")
                ),
                "note": gj.get("field_polygon_note"),
                "mean_score": gj.get("field_polygon_mean_score"),
            }
        )
    return out


@router.get("/api/fieldv2/{gid}")
def get_one(gid: str):
    gjp = _resolve(gid)[1]
    if not gjp or not os.path.exists(gjp):
        raise HTTPException(404, "no game.json for %s" % gid)
    gj = json.load(open(gjp, encoding="utf-8"))
    segs = gj.get("segments") or []
    mid = segs[len(segs) // 2] if segs else {}
    return {
        "game_id": gid,
        "polygon": gj.get("field_polygon"),
        "source": gj.get("field_polygon_source"),
        "verified": gj.get("field_polygon_verified"),
        "mean_score": gj.get("field_polygon_mean_score"),
        "note": gj.get("field_polygon_note"),
        "src_w": mid.get("w"),
        "src_h": mid.get("h"),
        "disp_w": DISP_W,
    }


@router.get("/api/fieldv2/{gid}/bg.jpg")
def background(gid: str):
    vd, gjp = _resolve(gid)
    if not gjp or not os.path.exists(gjp):
        raise HTTPException(404, "no game.json")
    gj = json.load(open(gjp, encoding="utf-8"))
    segs = gj.get("segments") or []
    f = _seg_file(vd, segs[len(segs) // 2]["seg"]) if segs else None
    if not f:
        f = _any_footage(vd)
    if not f:
        raise HTTPException(404, "no video for %s" % gid)
    img = _decode_mid(f)
    if img is None:
        raise HTTPException(404, "decode failed")
    # PyAV ignores the display-rotation tag (unlike cv2, which the polygon coords assume) -> apply it
    if gj.get("video_rotation") in (180, -180):
        img = img.transpose(Image.ROTATE_180)
    w, h = img.size
    disp = img.resize((DISP_W, max(1, int(h * DISP_W / w))))
    buf = io.BytesIO()
    disp.save(buf, format="JPEG", quality=85)
    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers={"X-Src-W": str(w), "X-Src-H": str(h), "Cache-Control": "no-store"},
    )


@router.post("/api/fieldv2/{gid}")
async def save(gid: str, request: Request):
    gjp = _resolve(gid)[1]
    if not gjp or not os.path.exists(gjp):
        raise HTTPException(404, "no game.json")
    body = await request.json()
    poly = body.get("polygon")
    if not poly or len(poly) < 4:
        raise HTTPException(
            400, "need >= 4 points (got %s)" % (len(poly) if poly else 0)
        )
    gj = json.load(open(gjp, encoding="utf-8"))
    gj["field_polygon"] = [[round(float(x), 1), round(float(y), 1)] for x, y in poly]
    gj["field_polygon_source"] = "human_field_edit"
    gj["field_polygon_verified"] = True
    gj.pop("field_polygon_note", None)
    tmp = gjp + ".tmp"
    json.dump(gj, open(tmp, "w", encoding="utf-8"), indent=1)
    os.replace(tmp, gjp)
    return {"ok": True, "game_id": gid, "points": len(poly)}


# ===================== game_state (phase) editor =====================
# Verify/correct the 5 game phases by scrubbing the boundary frames. Reads/writes game.json
# game_state ([{phase, start:[seg,f], end:[seg,f], source}]). play_windows-seeded boundaries are
# offset (upload-time vs recording-time), so the human scrubs each boundary to the true frame.
import bisect  # noqa: E402

PHASES = ("pre_game", "first_half", "halftime", "second_half", "post_game")


def _seg_index(gj):
    segs = gj.get("segments") or []
    offs = [int(s["global_offset"]) for s in segs]
    names = [s["seg"] for s in segs]
    frs = [int(s["frames"]) for s in segs]
    return offs, names, frs


def _g2sf(g0, offs, names, frs):
    total = sum(frs)
    g0 = max(0, min(total - 1, g0)) if total else 0
    i = bisect.bisect_right(offs, g0) - 1
    if i < 0:
        i = 0
    floc = g0 - offs[i]
    if frs[i] and floc >= frs[i]:
        floc = frs[i] - 1
    return [names[i], int(floc)]


def _sf2g(seg, f, offs, names):
    if seg in names:
        return offs[names.index(seg)] + int(f)
    return int(f)


def _decode_frame(path, f, fps):
    c = av.open(path)
    try:
        st = c.streams.video[0]
        st.thread_type = "AUTO"
        t = f / (fps or 25.0)
        if st.time_base and t > 0:
            c.seek(int(t / st.time_base), stream=st, backward=True)
        for fr in c.decode(st):
            return fr.to_image()
    finally:
        c.close()
    return None


@router.get("/api/phasesv2/games")
def phases_games():
    out = []
    for g in _games():
        gjp = _resolve(g["game_id"])[1]
        if not gjp or not os.path.exists(gjp):
            continue
        gj = json.load(open(gjp, encoding="utf-8"))
        ps = gj.get("game_state") or []
        srcs = {p.get("source") for p in ps}
        out.append(
            {
                "game_id": g["game_id"],
                "trainable": bool(g.get("trainable")),
                "has_phases": len(ps) > 0,
                "verified": len(ps) > 0 and srcs <= {"human", "whistle"},
                "source": ",".join(sorted(s for s in srcs if s)) or None,
            }
        )
    return out


@router.get("/api/phasesv2/{gid}")
def phases_get(gid: str):
    gjp = _resolve(gid)[1]
    if not gjp or not os.path.exists(gjp):
        raise HTTPException(404, "no game.json")
    gj = json.load(open(gjp, encoding="utf-8"))
    offs, names, frs = _seg_index(gj)
    total = sum(frs)
    fps = (gj.get("segments") or [{}])[0].get("fps") or 25.0
    ps = gj.get("game_state") or []

    def bound(phase, which):
        for p in ps:
            if p["phase"] == phase:
                seg, f = p[which]
                return _sf2g(seg, f, offs, names)
        return None

    b = {
        "kickoff": bound("first_half", "start"),
        "half_start": bound("halftime", "start"),
        "half_end": bound("second_half", "start"),
        "end": bound("post_game", "start"),
    }
    if not ps and total:  # no phases yet -> sensible default scrub positions
        b = {
            "kickoff": int(total * 0.05),
            "half_start": int(total * 0.45),
            "half_end": int(total * 0.52),
            "end": int(total * 0.95),
        }
    return {
        "game_id": gid,
        "total_frames": total,
        "fps": fps,
        "boundaries": b,
        "source": (ps[0]["source"] if ps else None),
    }


@router.get("/api/phasesv2/{gid}/frame")
def phases_frame(gid: str, g: int = 0):
    vd, gjp = _resolve(gid)
    if not gjp or not os.path.exists(gjp):
        raise HTTPException(404, "no game.json")
    gj = json.load(open(gjp, encoding="utf-8"))
    offs, names, frs = _seg_index(gj)
    fps = (gj.get("segments") or [{}])[0].get("fps") or 25.0
    sf = _g2sf(int(g), offs, names, frs)
    f = _seg_file(vd, sf[0])
    if not f:
        raise HTTPException(404, "no segment file")
    img = _decode_frame(f, sf[1], fps)
    if img is None:
        raise HTTPException(404, "decode failed")
    if gj.get("video_rotation") in (180, -180):
        img = img.transpose(Image.ROTATE_180)
    img = img.resize((960, max(1, int(img.height * 960 / img.width))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/phasesv2/{gid}")
async def phases_save(gid: str, request: Request):
    gjp = _resolve(gid)[1]
    if not gjp or not os.path.exists(gjp):
        raise HTTPException(404, "no game.json")
    gj = json.load(open(gjp, encoding="utf-8"))
    offs, names, frs = _seg_index(gj)
    total = sum(frs)
    b = (await request.json()).get("boundaries") or {}
    try:
        kf, hs, he, en = (
            int(b["kickoff"]),
            int(b["half_start"]),
            int(b["half_end"]),
            int(b["end"]),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(
            400, "need integer boundaries kickoff/half_start/half_end/end"
        ) from e

    def sf(g0):
        return _g2sf(g0, offs, names, frs)

    gj["game_state"] = [
        {"phase": "pre_game", "start": [names[0], 0], "end": sf(kf), "source": "human"},
        {"phase": "first_half", "start": sf(kf), "end": sf(hs), "source": "human"},
        {"phase": "halftime", "start": sf(hs), "end": sf(he), "source": "human"},
        {"phase": "second_half", "start": sf(he), "end": sf(en), "source": "human"},
        {
            "phase": "post_game",
            "start": sf(en),
            "end": sf(total - 1),
            "source": "human",
        },
    ]
    tmp = gjp + ".tmp"
    json.dump(gj, open(tmp, "w", encoding="utf-8"), indent=1)
    os.replace(tmp, gjp)
    return {"ok": True, "game_id": gid, "phases": 5}
