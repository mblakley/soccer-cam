"""Field-boundary editor backed by the canonical per-game ``game.json`` sidecars (next to each
video on F:), independent of the legacy ``manifest.db`` / ``v4_fields`` store. Lets a human review
and drag-correct the 10-point field polygon for ANY registry game (including the auto-seeded,
``field_polygon_verified=false`` ones), with the canvas background decoded straight from the video.

Mounted into the annotation server via ``app.include_router(field_edit_v2.router)``; the page lives at
``/static/field-edit.html`` and talks to ``/api/fieldv2/*``. Saving sets
``field_polygon_source='human_field_edit'`` + ``field_polygon_verified=true``.
"""

import glob
import json
import os

import cv2
from fastapi import APIRouter, HTTPException, Request, Response

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


@router.get("/api/fieldv2/games")
def games_list():
    """All registry games that have a game.json, with polygon/verify status (for the picker)."""
    out = []
    for g in _games():
        gjp = _resolve(g["game_id"])[1]
        if not gjp or not os.path.exists(gjp):
            continue
        gj = json.load(open(gjp, encoding="utf-8"))
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
    cap = cv2.VideoCapture(f)
    tot = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 100
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * 0.5))
    ok, fr = cap.read()
    cap.release()
    if not ok or fr is None:
        raise HTTPException(404, "decode failed")
    h, w = fr.shape[:2]
    disp = cv2.resize(fr, (DISP_W, int(h * DISP_W / w)))
    ok, buf = cv2.imencode(".jpg", disp, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return Response(
        content=buf.tobytes(),
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
