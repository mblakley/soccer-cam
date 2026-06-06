# soccer-cam-ios — Swift app architecture

Spec for the iOS / iPadOS app that runs the soccer-cam detect / track /
render pipeline against a recorded Reolink panorama, with streaming-segment
processing and TTT integration. Per the iOS-port plan: OSS, free, with
premium TTT-licensed models gated server-side.

## Layering

```
┌─────────────────────────────────────────────────────────────┐
│ UI                       (SwiftUI, @MainActor)              │
│  GamesListView, GameDetailView, ImportFlowView,             │
│  SignInView, SettingsView                                   │
├─────────────────────────────────────────────────────────────┤
│ View models              (ObservableObject, @MainActor)     │
│  GamesListViewModel, GameDetailViewModel                    │
├─────────────────────────────────────────────────────────────┤
│ Domain actors            (Swift actor)                      │
│  GameManager → [Game] → [Segment]                           │
├─────────────────────────────────────────────────────────────┤
│ Services                                                    │
│  AuthService, ModelCatalog, SegmentDownloader,              │
│  TTTAPIClient, CryptoKitLoader                              │
├─────────────────────────────────────────────────────────────┤
│ Per-segment pipeline     (Task, spawned by Game)            │
│  VideoDecoder → BallDetector → BallTracker →                │
│  CameraStateMachine → MetalWarpRenderer → VideoEncoder      │
├─────────────────────────────────────────────────────────────┤
│ Platform                                                    │
│  AVFoundation  CoreML/Vision  Metal  VideoToolbox  CryptoKit│
└─────────────────────────────────────────────────────────────┘
```

## Module layout (Swift Package + Xcode app target)

```
SoccerCamIOS/
├── App/                       # SwiftUI app entry + theming
│   ├── SoccerCamApp.swift     # @main
│   ├── RootView.swift
│   └── Theme.swift
├── Features/                  # one folder per top-level screen
│   ├── GamesList/
│   ├── GameDetail/
│   ├── ImportFlow/
│   ├── SignIn/
│   └── Settings/
├── Domain/                    # game / segment model + persistence
│   ├── GameManager.swift      # actor — owns active games
│   ├── Game.swift             # actor — one per game in progress
│   ├── Segment.swift          # struct — per-segment state machine
│   ├── Manifest.swift         # game_manifest.json schema + load/save
│   └── CarryoverState.swift   # tracker + camera state across segments
├── Services/
│   ├── Auth/
│   │   ├── AuthService.swift          # OAuth via ASWebAuthenticationSession
│   │   └── TokenStore.swift           # Keychain wrapper
│   ├── ModelCatalog/
│   │   ├── ModelCatalog.swift         # community + TTT-free + TTT-premium
│   │   ├── BundledModels.swift        # paths to .mlpackage in app bundle
│   │   └── TTTModelClient.swift       # entitlement check + download
│   ├── ReolinkIngest/
│   │   ├── SegmentDownloader.swift    # actor — polls Reolink, queues segments
│   │   └── ReolinkAPI.swift           # /api/cgi list/get
│   ├── TTT/
│   │   ├── TTTAPIClient.swift         # URLSession against TTT REST API
│   │   └── VideoUpload.swift          # POST rendered mp4 to game
│   └── Decryption/
│       └── CryptoKitLoader.swift      # AES-GCM, in-memory only
├── Pipeline/
│   ├── SegmentProcessor.swift         # orchestrates detect→track→render
│   ├── BallDetector.swift             # CoreML/Vision tile pipeline
│   ├── BallTracker.swift              # pure-Swift Kalman (port of ball_tracker.py)
│   ├── CameraStateMachine.swift       # port of render.py _tick / _frame_view
│   ├── CylindricalView.swift          # port of cylindrical_view.py
│   ├── FieldGeometry.swift            # port of field_geometry.py
│   ├── MetalWarpRenderer.swift        # dispatches WarpKernel
│   ├── VideoDecoder.swift             # AVAssetReader → CVPixelBuffer
│   └── VideoEncoder.swift             # VideoToolbox H.264 → .mp4
├── Metal/
│   ├── WarpKernel.metal               # the warp compute shader
│   └── MetalDevice.swift              # shared MTLDevice + command queue
├── Storage/
│   ├── FileManagerExtensions.swift
│   ├── JSONManifest.swift             # atomic Codable read/write
│   └── SandboxPaths.swift             # app group container layout
└── Resources/
    ├── Models/                        # bundled community .mlpackage
    └── Assets.xcassets                # custom icon + splash (per [[feedback_custom_branding_from_start]])
```

## Concurrency model

**Top-level actors:**

- `actor GameManager` — singleton owned by app delegate. Tracks the set of
  active Games. Persisted as `games_index.json`. UI views observe a snapshot
  published on `@MainActor`.
- `actor Game` — one per game-in-progress. Owns its `SegmentDownloader`,
  the segment lifecycle state, and the `CarryoverState` between segments.
- `actor SegmentDownloader` — per-game; polls a Reolink camera (or a
  bulk-imported file's virtual chunker) at a configurable cadence.

**Per-segment processing:**

A `Segment` does NOT get its own actor — instead `Game.processSegment(_:)`
spawns an unstructured `Task` for each ready segment. Within that task, the
pipeline calls are serial (detect → track → render); inference + Metal
dispatch + encode each move work to their platform-owned queues, but the
overall pipeline flow stays sequential because state carries forward
(tracker state, camera state). One Game processes at most one segment at
a time; multiple Games could in principle run in parallel but iOS thermals
make that a non-goal — `GameManager` serializes Games.

**MainActor boundaries:**

All `@Published` properties on view models live on `@MainActor`. Actors
broadcast state changes via `AsyncStream` that view models consume with
`for await ... in stream` inside a `.task` modifier.

**Cancellation:**

When the user cancels a Game (or the app is force-killed), `Game.cancel()`
calls `task.cancel()` on the running per-segment Task. The pipeline checks
`Task.isCancelled` at safe points (between detect/track/render stages,
between segments). Partial outputs are discarded — never half-flushed mp4s.

## Persistence

Per-game on-disk layout under the app sandbox's `Documents/games/<gameId>/`:

```
<gameId>/
├── manifest.json              # game_manifest.json (see data_model.md)
├── segments/
│   ├── segment_001.mp4        # raw segment (deleted post-render)
│   ├── segment_002.mp4
│   └── ...
├── rendered/
│   ├── rendered_001.mp4       # per-segment rendered output
│   ├── rendered_002.mp4
│   └── ...
├── carryover/
│   └── carryover_001.json     # state at end of segment N → start of N+1
├── final.mp4                  # concatenated final output (post-game)
└── debug/                     # only when debug logging enabled
    └── detections_001.json
```

Encrypted model artifacts live in `Library/Caches/models/` (excluded from
iCloud backup; can be re-downloaded). Decrypted plaintext NEVER touches disk
— `CryptoKitLoader.decrypt` returns `Data` that goes straight to
`MLModel(contentsOf:)` via a temp file in a wiped directory, OR ideally
straight to `try MLModel(modelData:)` if that path exists in the iOS SDK
version. See `cryptokit_decryption.md`.

## Background processing

Long renders need `BGProcessingTask` so iOS doesn't suspend us. Register
`com.soccercam.ios.render` with `requiresExternalPower=false` (we want to
work on-battery in the field), `requiresNetworkConnectivity=true` (for
Reolink polling). The processor schedules a new task each time it completes
or backgrounds. App also opts into the audio background mode as a defensive
keepalive when actively decoding — render uses audio passthrough so this
is honest, not a workaround.

## Dependencies

**Apple SDKs only.** No third-party Swift packages in the MVP. Specifically:

- `SwiftUI`, `Combine` — UI
- `AVFoundation`, `VideoToolbox`, `CoreVideo` — decode/encode/pixel buffers
- `CoreML`, `Vision` — inference
- `Metal`, `MetalKit` — warp compute shader
- `CryptoKit` — AES-GCM model decryption
- `Security` — Keychain (token storage)
- `AuthenticationServices` — `ASWebAuthenticationSession` for TTT OAuth
- `BackgroundTasks` — `BGProcessingTask` for long renders

A third-party package only enters if a specific iOS-port phase justifies it
(e.g. GStreamer if Phase 8 real-time mode needs RTSP). MVP stays pure-Apple.

## Test layout

```
Tests/
├── PipelineTests/             # parity vs ios-port-prep/baselines/
│   ├── BallTrackerTests.swift
│   ├── CylindricalViewTests.swift
│   ├── CameraStateMachineTests.swift
│   └── WarpKernelTests.swift
├── DomainTests/               # Game/Segment lifecycle
├── ServicesTests/             # mocked TTT + Reolink
└── UITests/                   # screenshot diffs for golden screens
```

Each pipeline test loads the corresponding baseline file from
`ios-port-prep/baselines/` (vendored into the test bundle as resources) and
compares the Swift output against it per the tolerance defined in the
matching E0.B-track experiment.

## Cross-references

- `data_model.md` — every JSON schema this code reads/writes
- `swift_kalman_tracker.md` — port spec for `BallTracker.swift`
- `swift_projection_math.md` — port spec for `CylindricalView.swift`
- `swift_camera_state_machine.md` — port spec for `CameraStateMachine.swift`
- `metal_warp_shader.md` — `WarpKernel.metal` spec
- `cryptokit_decryption.md` — `CryptoKitLoader.swift` spec
- `ttt_api_integration.md` — `TTTAPIClient.swift` spec
- `reolink_segment_ingest.md` — `SegmentDownloader.swift` spec
- `segment_pipeline.md` — `SegmentProcessor.swift` lifecycle
- `app_ui.md` — screen-by-screen UI spec
- `app_store_plan.md` — submission strategy
