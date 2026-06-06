# Data model — JSON schemas

All persisted state in soccer-cam-ios is JSON, written via
`JSONManifest.swift` (atomic temp-file write + rename). Schemas below define
the exact field names; the Swift `Codable` types in `Domain/` mirror them
1:1. All numeric IDs are strings (UUIDs) so the schemas are stable across
SQLite indexing changes if we ever migrate to a DB.

## Versioning

Every schema carries `schema_version: Int` at the top level. The MVP ships
at v1. Migration policy: any schema bump ships a `Migrations.swift` that
maps vN → vN+1, called from `JSONManifest.load` before decoding. Per
[[feedback_pre_launch_no_backcompat]] this stays clean — we make breaking
changes, write the migration, no nullable legacy aliases.

## game_manifest.json

The per-game state file. Path:
`Documents/games/<gameId>/manifest.json`.

```json
{
  "schema_version": 1,
  "game_id": "01HXYZ...",                  // ULID, app-generated
  "ttt_game_id": "uuid-or-null",           // set after TTT upload; null otherwise
  "display_name": "Flash vs Heat 2026-06-15 10:00",
  "created_at": "2026-06-15T10:00:00Z",
  "completed_at": null,                    // ISO timestamp when game finalized
  "status": "downloading",                 // see Game.Status enum below

  "source": {
    "kind": "reolink",                     // "reolink" | "bulk_import"
    "reolink": {
      "base_url": "https://192.168.1.42",
      "username": "admin",
      "channel": 0
    },
    "bulk_import": null                    // if kind=="bulk_import":
                                           // {"original_filename": "...", "imported_at": "..."}
  },

  "settings": {
    "model_source": {
      "kind": "bundled",                   // "bundled" | "ttt_free" | "ttt_premium"
      "model_id": "community-ball-v3"
    },
    "render_mode": "broadcast",            // "broadcast" | "coach"
    "output_resolution": [1920, 1080]
  },

  "segments": [
    /* one entry per segment, see segment schema below */
  ],

  "final_output": {
    "path": "rendered/final.mp4",          // relative to game dir; null until complete
    "duration_seconds": null,
    "uploaded_to_ttt": false,
    "uploaded_video_id": null
  }
}
```

### Game.Status enum

| Value | Meaning |
|-------|---------|
| `pending` | Created, not yet started |
| `downloading` | Actively polling source for new segments |
| `processing` | Source done, segments still rendering |
| `complete` | All segments rendered + final concatenated |
| `uploaded` | Final mp4 pushed to TTT (terminal happy state) |
| `cancelled` | User-cancelled (terminal) |
| `failed` | Unrecoverable error (terminal); see `error` field |

When `status == "failed"`, additional top-level field:

```json
"error": {
  "code": "MODEL_LICENSE_REVOKED",
  "message": "Premium model license check failed (HTTP 403)",
  "occurred_at": "2026-06-15T11:23:45Z",
  "failed_segment_id": "segment_007"        // null if not segment-specific
}
```

## segment schema (inside `game_manifest.json#/segments`)

```json
{
  "segment_id": "segment_001",             // sequential, zero-padded
  "sequence": 1,                           // monotonic int
  "status": "rendered",                    // see Segment.Status enum below

  "source_path": "segments/segment_001.mp4",   // relative; null after deletion
  "rendered_path": "rendered/rendered_001.mp4", // null until rendered
  "carryover_path": "carryover/carryover_001.json", // produced at end of segment

  "source_bytes": 524288000,
  "source_duration_seconds": 300.0,
  "source_started_at": "2026-06-15T10:00:00Z",   // segment's wall-clock start

  "frame_count": 9000,                     // populated after decode walk
  "detection_count": 1247,                 // populated after detect

  "timings_ms": {
    "downloaded_ms": 45000,                // wall-clock per stage
    "detect_ms": 78000,
    "track_ms": 350,
    "render_ms": 95000
  },

  "started_at": "2026-06-15T10:00:30Z",
  "completed_at": "2026-06-15T10:04:15Z"
}
```

### Segment.Status enum

| Value | Meaning |
|-------|---------|
| `pending_download` | Known to exist on source, not yet downloaded |
| `downloading` | In-flight |
| `ready_to_process` | Downloaded, queued for pipeline |
| `detecting` | Ball detection running |
| `tracking` | Tracker running (always sub-second; rarely seen) |
| `rendering` | Camera state machine + Metal warp + encode |
| `rendered` | Per-segment mp4 written; raw source can be deleted |
| `discarded` | Source + intermediates deleted (terminal happy state) |
| `failed` | This segment errored; subsequent segments still attempt to run |

## carryover_NNN.json

The state passed from segment N to segment N+1 so the tracker and the camera
state machine don't reset at every segment boundary. Path:
`Documents/games/<gameId>/carryover/carryover_<seqNNN>.json`.

```json
{
  "schema_version": 1,
  "produced_by_segment": "segment_001",
  "produced_at": "2026-06-15T10:04:15Z",
  "last_frame_idx": 8999,                  // last absolute frame processed

  "active_tracks": [
    {
      "track_id": 7,                        // stable ID across the whole game
      "kalman_state": {
        "x": [102.4, 543.1, 5.0, -1.2, 0.0, 0.0],   // [x, y, vx, vy, ax, ay]
        "P": [                                       // 6×6 row-major
          [2500.0, 0.0, ...],
          ...
        ]
      },
      "missing_frames": 0,
      "last_seen_frame_idx": 8997
    }
  ],
  "next_track_id": 12,                     // ID counter so new tracks get unique IDs

  "camera_state": {
    "smoothed_yaw_deg": 12.4,
    "smoothed_pitch_deg": -3.1,
    "smoothed_zoom_frac": 0.18,             // crop-width fraction of src HFOV
    "stationary_frames": 3,
    "missing_frames": 0,
    "last_velocity_px_per_frame": [4.2, -1.1]   // [vx, vy] for EMA continuity
  },

  "world_up_pano": {
    "computed_at_segment": "segment_001",  // we only compute this once per game
    "mount_tilt_deg": 18.7,
    "leveling_roll_deg": 0.3,
    "field_polygon": [
      [231.0, 1402.0], [3865.0, 1398.0], [3982.0, 1701.0], [122.0, 1709.0]
    ]
  }
}
```

When `active_tracks` is empty or `world_up_pano` is null, the next segment
starts cold for that aspect — degrades gracefully, doesn't fail.

## games_index.json

App-wide index of games. Path: `Documents/games_index.json`.

```json
{
  "schema_version": 1,
  "games": [
    {
      "game_id": "01HXYZ...",
      "display_name": "Flash vs Heat 2026-06-15 10:00",
      "status": "uploaded",
      "created_at": "2026-06-15T10:00:00Z",
      "updated_at": "2026-06-15T12:15:00Z",
      "thumbnail_path": "games/01HXYZ.../thumb.jpg"  // first-render-frame thumbnail
    }
  ]
}
```

## detections.json (per-segment, debug-only on iOS)

Matches the Python schema bit-for-bit (sort_keys output from
soccer-cam's `detect` step) so the Swift detector can compare directly to
`ios-port-prep/baselines/<clip>/detections.json`. NOT persisted in production
runs — only when the user enables debug logging. Path:
`Documents/games/<gameId>/debug/detections_<seqNNN>.json`.

```json
[
  {
    "frame_idx": 0,
    "cx": 2014.3,
    "cy": 891.7,
    "w": 22.1,
    "h": 21.8,
    "conf": 0.87
  },
  ...
]
```

## trajectory.json (per-segment, debug-only)

Matches the Python schema. Each entry is `[x, y]` or `null` per source frame.

```json
[[102.3, 543.1], [105.4, 542.9], null, null, [120.1, 540.0], ...]
```

## camera_states.json (debug-only)

Matches the Python parity dump from `render.py`'s `_dump_frame_state`. The
Swift renderer writes the same schema so E0.B4 (camera state machine
parity) is a direct file diff.

```json
[
  {
    "frame_idx": 0,
    "view_yaw_deg": 0.0,
    "smoothed_yaw_deg": 0.0,
    "smoothed_pitch_deg": null,
    "smoothed_zoom_frac": 0.22,
    "view_hfov_deg": 47.0,
    "view_pitch_deg": 0.0,
    "stationary_frames": 0,
    "missing_frames": 0
  },
  ...
]
```

## Token / secret storage (Keychain, NOT JSON)

```
Keychain account="ttt_access_token"     value=<JWT>
Keychain account="ttt_refresh_token"    value=<refresh>
Keychain account="reolink/<host>"       value=<password>  // per-camera
```

Encrypted model artifacts go under `Library/Caches/models/` with filename
`<model_id>_<version>.enc` (the encrypted blob). Decryption keys are derived
per-session from the JWT — they are never persisted anywhere.

## Schema-evolution rules

- Adding an optional field with a sensible default: no version bump, no
  migration. Decoder tolerates absence.
- Adding a required field, removing a field, or changing semantics: bump
  `schema_version`, ship a migration.
- Renaming a field: ship as add-new + migrate + remove-old across two
  versions. Even pre-launch, this is cheap insurance against partially-
  upgraded test devices.

Per [[feedback_pre_launch_no_backcompat]] we don't ship deprecated aliases
or nullable legacy columns — migrations are the *only* mechanism.
