"""Game active-play window labeler (web): scrub the embedded Full Field YouTube video and capture
kickoff / halftime-start / halftime-end / final-whistle for each game. Writes
G:/ballresearch/play_windows.json (windows in frames @ 20 fps) -- the active-play windows the
training curve consumes, in the SAME schema as add_play_windows.py:
   {fps, start, half_start, half_end, end, windows}
No YouTube API/token needed (the embed plays in the browser).

YouTube auto-matching is unreliable (duplicate opponents share identical titles; upload date != game
date), so the tool SUGGESTS candidate videos and the user confirms/picks/pastes the URL. The confirmed
choice is remembered in G:/ballresearch/game_youtube_map.json (kept OUT of play_windows.json for schema
consistency). Wired in via `register_game_windows(app)` (appended to annotation_server.py).
"""

import json
import os
import re
from pathlib import Path

from fastapi import HTTPException

PLAY_WINDOWS = Path("G:/ballresearch/play_windows.json")
YT_MAP = Path("G:/ballresearch/game_youtube_map.json")   # {game_id: video_id} confirmed by the user
YOUTUBE = Path("G:/ballresearch/youtube_uploads.json")
ARCH = Path("F:/archive/ball_distill")
STORAGE = Path("D:/soccer-cam-storage")
FPS = 20
HELD_OUT = "spencerport"
_STOP = {"sc", "fc", "the", "vs", "bu14", "bu13", "bu15", "u15", "u14", "u13", "guzzetta", "flash", "hilton", "heat"}


def _load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _write_json(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40]


def _gid_from_storage(gname, dirname):
    m = re.search(r"(2026\.\d{2}\.\d{2})", gname) or re.search(r"(2026\.\d{2}\.\d{2})", dirname)
    date = m.group(1) if m else os.path.basename(dirname)[:10]
    low = gname.lower()
    team = "guzzetta" if "guzzetta" in low else ("flash" if "flash" in low else "team")
    v = re.search(r"\bvs\b\s+(.+?)(?:\s*\(|$)", gname, re.I)
    return f"{team}__{date}_vs_{_slug(v.group(1)) if v else 'opponent'}"


def _storage_games():
    out = []
    if not STORAGE.exists():
        return out
    for d in sorted(STORAGE.glob("2026*")):
        if not d.is_dir() or not list(d.glob("RecM09_*.mp4")):
            continue
        subs = [x for x in d.glob("*") if x.is_dir()]
        if subs:
            out.append(_gid_from_storage(subs[0].name, str(d)))
    return out


def _archived_games():
    if not ARCH.exists():
        return []
    return [d.name for d in sorted(ARCH.iterdir()) if d.is_dir() and (d / "ball_track.json").exists()]


def _words(s):
    return [w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if w]


def _opp_tokens(gid):
    opp = gid.split("_vs_")[-1] if "_vs_" in gid else ""
    return [w for w in _words(opp) if w not in _STOP]


def _candidates(gid, ups):
    """Ranked candidate videos for a game: whole-word opponent overlap, Full Field preferred.
    Upload date is NOT used (it's the upload date, not the game date)."""
    toks = set(_opp_tokens(gid))
    scored = []
    for u in ups:
        twords = set(_words(u.get("title")))
        overlap = len(toks & twords)
        if overlap == 0:
            continue
        title = (u.get("title") or "").lower()
        score = overlap * 10
        if "full field" in title:
            score += 5
        if "raw" in title:
            score -= 3
        scored.append((score, u))
    scored.sort(key=lambda x: (-x[0], x[1].get("date", "")))
    return [u for _, u in scored]


def _mmss(x):
    x = int(round(x))
    if x >= 3600:
        return f"{x // 3600}:{(x % 3600) // 60:02d}:{x % 60:02d}"
    return f"{x // 60}:{x % 60:02d}"


def _verify_kickoff(gid, kickoff_frame):
    p = ARCH / gid / "ball_track.json"
    if not p.exists():
        return {"archived": False, "note": "not archived yet -- windows saved, alignment will be checkable after detection"}
    try:
        import numpy as np
    except Exception:
        return {"archived": True, "note": "numpy unavailable; alignment check skipped"}
    try:
        bt = json.loads(p.read_text())
        segs = bt["segments"]
        nseg = max(s["index"] for s in segs) + 1
        nf = {s["index"]: s["n_frames"] for s in segs}
        offs, acc = {}, 0
        for i in range(nseg):
            offs[i] = acc
            acc += nf.get(i, 0)
        tot = acc
        tx = np.full(tot, np.nan)
        for s in segs:
            for f, xy in (s.get("track") or {}).items():
                g = offs[s["index"]] + int(f)
                if 0 <= g < tot:
                    tx[g] = xy[0]

        def act(lo, hi):
            xs = tx[max(0, lo):min(tot, hi)]
            ok = np.isfinite(xs)
            cov = float(ok.mean()) if len(xs) else 0.0
            d = float(np.nanmean(np.abs(np.diff(xs[ok])))) if ok.sum() > 3 else 0.0
            return [round(cov, 2), round(d, 1)]

        before = act(kickoff_frame - 600, kickoff_frame)
        after = act(kickoff_frame, kickoff_frame + 600)
        aligned = (after[0] > before[0] + 0.1) or (after[1] > before[1] * 1.3)
        return {"archived": True, "before_cov_disp": before, "after_cov_disp": after,
                "aligned": bool(aligned), "total_frames": int(tot)}
    except Exception as e:  # noqa: BLE001
        return {"archived": True, "error": str(e)}


def register_game_windows(app):
    @app.get("/api/game-windows")
    def list_game_windows():
        ups = _load_json(YOUTUBE, [])
        pw = _load_json(PLAY_WINDOWS, {})
        ymap = _load_json(YT_MAP, {})
        archived = set(_archived_games())
        gids = sorted(set(_storage_games()) | archived)
        rows = []
        for gid in gids:
            if HELD_OUT in gid.lower():
                continue
            cands = _candidates(gid, ups)
            vid = ymap.get(gid) or (cands[0].get("video_id") if cands else None)
            existing = pw.get(gid) or {}
            rows.append({
                "game_id": gid,
                "video_id": vid,
                "confirmed": gid in ymap,
                "n_candidates": len(cands),
                "archived": gid in archived,
                "has_windows": bool(existing.get("windows")),
            })
        rows.sort(key=lambda r: (r["has_windows"], r["game_id"]))
        return {"games": rows, "needs_windows": sum(1 for r in rows if not r["has_windows"])}

    @app.get("/api/game-windows/{game_id}")
    def get_game_window(game_id: str):
        ups = _load_json(YOUTUBE, [])
        pw = _load_json(PLAY_WINDOWS, {})
        ymap = _load_json(YT_MAP, {})
        cands = _candidates(game_id, ups)
        confirmed = ymap.get(game_id)
        existing = pw.get(game_id) or {}
        return {
            "game_id": game_id,
            "confirmed_video_id": confirmed,
            "suggested_video_id": confirmed or (cands[0].get("video_id") if cands else None),
            "candidates": [{"video_id": c.get("video_id"), "title": c.get("title"), "date": c.get("date")} for c in cands[:12]],
            "archived": (ARCH / game_id / "ball_track.json").exists(),
            "existing": {k: existing.get(k) for k in ("start", "half_start", "half_end", "end")},
        }

    @app.post("/api/game-windows/{game_id}")
    async def save_game_window(game_id: str, body: dict):
        try:
            ts = {k: float(body[k]) for k in ("start", "half_start", "half_end", "end")}
        except Exception:
            raise HTTPException(400, "need numeric seconds: start, half_start, half_end, end")
        if not (ts["start"] < ts["half_start"] <= ts["half_end"] < ts["end"]):
            raise HTTPException(400, "need start < half_start <= half_end < end")
        windows = [[int(ts["start"] * FPS), int(ts["half_start"] * FPS)],
                   [int(ts["half_end"] * FPS), int(ts["end"] * FPS)]]
        # standard schema only (matches add_play_windows.py / existing games)
        pw = _load_json(PLAY_WINDOWS, {})
        pw[game_id] = {
            "fps": FPS,
            "start": _mmss(ts["start"]),
            "half_start": _mmss(ts["half_start"]),
            "half_end": _mmss(ts["half_end"]),
            "end": _mmss(ts["end"]),
            "windows": windows,
        }
        _write_json(PLAY_WINDOWS, pw)
        # remember which video was used, OUT of play_windows.json (schema consistency)
        vid = body.get("video_id")
        if vid:
            ymap = _load_json(YT_MAP, {})
            ymap[game_id] = vid
            _write_json(YT_MAP, ymap)
        return {"ok": True, "game_id": game_id, "windows": windows,
                "verify": _verify_kickoff(game_id, windows[0][0]), "n_games": len(pw)}
