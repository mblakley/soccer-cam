# ios-port-prep — Mac-handoff package

Pre-built Windows-side deliverable for the soccer-cam → iOS port. Produced
during Phase W of the iOS-port plan
(`.claude/plans/based-on-the-code-vectorized-chipmunk.md`). Drop this
directory onto the Mac alongside the soccer-cam repo to start Phase 0 on
device without re-deriving anything.

## What's here

```
ios-port-prep/
├── README.md                       # this file
├── PHASE_0_KICKOFF.md              # Mac-side runbook — read this first
├── design/                         # W.5 — 12 Swift / iOS design specs
│   ├── README.md                   # design-dir map (read order)
│   ├── architecture.md
│   ├── data_model.md
│   ├── swift_kalman_tracker.md
│   ├── swift_projection_math.md
│   ├── swift_camera_state_machine.md
│   ├── metal_warp_shader.md
│   ├── cryptokit_decryption.md
│   ├── ttt_api_integration.md
│   ├── reolink_segment_ingest.md
│   ├── segment_pipeline.md
│   ├── app_ui.md
│   └── app_store_plan.md
├── sources/                        # W.6 — skeleton Swift + Metal sources
│   ├── README.md                   # what's pre-written, what's TODO
│   ├── App/SoccerCamApp.swift
│   ├── Domain/{CarryoverState,Manifest}.swift
│   ├── Pipeline/{BallTracker,CameraStateMachine,CylindricalView,
│   │             SegmentProcessor}.swift
│   ├── Metal/WarpKernel.metal
│   └── Services/{Decryption/CryptoKitLoader,
│                  TTT/TTTAPIClient}.swift
├── models/                         # W.2 — model weight pointers
│   └── README.md                   # source paths + Mac CoreML-export commands
├── golden/                         # W.3 — test data (mp4s gitignored)
│   ├── README.md                   # source pointers + label hints
│   ├── short_clips/                # 30s extractions (gitignored)
│   ├── full_segment/               # one 5-min segment (gitignored)
│   ├── segment_pair_10min/         # two segments for E0.C2 (gitignored)
│   ├── hard_cases/                 # placeholder — Mark labels post-handoff
│   └── field_polygons/             # JSON polygons, small enough to commit
└── baselines/                      # W.4 — Phase 0 reference outputs
    └── segment1_first30s/          # produced by run_parity_harness.py
        ├── source.mp4              # (gitignored)
        ├── output.mp4              # (gitignored)
        ├── field_polygon.json
        ├── pipeline_state.json
        └── parity/                 # the committed baselines
            ├── detections.json
            ├── trajectory.json
            ├── leveled_pano_map_x.npy
            ├── leveled_pano_map_y.npy
            ├── leveled_pano_map_x.png    # visualization
            ├── leveled_pano_map_y.png
            ├── camera_states.json
            └── render_frame_NNNNNN.png   # 1/sec sampled
```

## Status of each W.x deliverable

| Sub-deliverable | Status | Notes |
|-----------------|--------|-------|
| W.1 — Parity harness | ✅ Complete (commit 87acf20) | 52 tests passing |
| W.2 — CoreML export | 🟡 Staged | `models/README.md` documents source paths + Mac export commands. `.mlpackage` build needs macOS. |
| W.3 — Golden test data | 🟡 Staged | 3× 30s clips extracted; field polygon staged; hard-case frames need human labeling on Mac |
| W.4 — Phase 0 baselines | 🟡 Partial | `segment1_first30s/parity/` produced by Windows run; remaining baselines run on Mac per `PHASE_0_KICKOFF.md` |
| W.5 — Design docs | ✅ Complete (commit cd8e68a) | 12 specs |
| W.6 — Skeleton Swift sources | ✅ Complete (commit 6933658) | 10 source files |
| W.7 — Package + kickoff | ✅ Complete (this commit) | `PHASE_0_KICKOFF.md` |

## What to read first on the Mac

1. `PHASE_0_KICKOFF.md` — what to do step-by-step.
2. `design/README.md` — orient on the 12 design docs.
3. `sources/README.md` — orient on what's pre-written in Swift.
4. `models/README.md` — fetch + CoreML-export instructions.
5. `golden/README.md` — fetch test recordings + the label-this-please prompt.

## What's intentionally NOT here

Per [[feedback_no_security_docs_in_oss]] + [[feedback_no_decrypted_onnx_in_oss]]:

- Threat models for the model-licensing scheme
- Forensic-watermarking rationale
- Specific TTT-licensed model identifiers / training-data details

Those live in the TTT private repo. The soccer-cam-ios repo (when initialized
on the Mac) inherits the same OSS posture as soccer-cam — see
[[feedback_client_apps_oss]].
