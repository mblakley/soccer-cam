# soccer-cam-ios — skeleton Swift sources

Pre-written Swift + Metal source files matching the W.5 design specs. These
**cannot build on Windows** (no Apple SDK linkage), but provide a head-start
for the Mac sessions: every file has type signatures, public API surface,
and `// TODO` bodies with inline references to the Python source they port.

## Layout

```
sources/
├── App/
│   └── SoccerCamApp.swift              # @main entry, GameManagerStore bridge
├── Domain/
│   ├── CarryoverState.swift            # Codable carry-over schema
│   └── Manifest.swift                  # game_manifest.json schema
├── Pipeline/
│   ├── BallTracker.swift               # port of ball_tracker.py (BYTE-IDENTICAL parity)
│   ├── CameraStateMachine.swift        # port of render.py _tick / _frame_view / CameraMode
│   ├── CylindricalView.swift           # port of cylindrical_view.py
│   └── SegmentProcessor.swift          # per-segment orchestration outline
├── Metal/
│   └── WarpKernel.metal                # the warp compute shader (verbatim from spec)
└── Services/
    ├── Decryption/
    │   └── CryptoKitLoader.swift       # AES-GCM model decryption
    └── TTT/
        └── TTTAPIClient.swift          # URLSession TTT client + AuthService stubs
```

## What's pre-written vs TODO

**Pre-written:**

- Complete type/struct/protocol signatures matching the design docs
- All constants (`BallTrackerConstants`, `CameraMode.broadcast`, etc.) with
  values transcribed verbatim from Python
- The Metal warp kernel (cannot test on Windows but the algorithm is final)
- Codable mappings for every JSON schema in `data_model.md` (field names
  + `CodingKeys` aligned to soccer-cam's snake_case)
- Inline references to Python source files / line ranges so the Mac
  sessions can pull up the reference next to the editor

**Marked `// TODO` for the Mac sessions:**

- Matrix algebra in `BallTracker` (`predict`, `update`) — needs Accelerate
  `cblas_dgemm` wired
- Per-pixel kernels in `CylindricalView` — needs the trig grid + cache
- The `_tick` branches in `CameraStateMachine` that the spec marks "port
  verbatim"
- AVFoundation glue in `VideoDecoder`, `SegmentProcessor`
- Metal host wrapper in `MetalWarpRenderer` (kernel itself is final)
- CoreML `MLModel(asset:)` load path in `CryptoKitLoader` after decryption
- `AuthService.signIn` (`ASWebAuthenticationSession` + PKCE)

## Order to flesh out (matches W-phase ordering on Mac)

1. **Phase 1 sanity:** confirm Mac reproduces Python baselines
2. **Phase 3 (detector):** `BallDetector` actor + CoreML wiring
3. **Phase 4 (tracker):** complete `BallTracker` + `KalmanMatrices` math,
   `writeTrajectory` (byte-identical parity test)
4. **Phase 5a (projection):** complete `CylindricalView`
5. **Phase 5b (state machine):** complete `CameraStateMachine.tickInner`
6. **Phase 5c (Metal warp):** complete `MetalWarpRenderer` host wrapper
7. **Phase 5d (carry-over + concat):** `SegmentProcessor.makeCarryover`,
   `Game.finalize` for the AVAssetExportSession concat
8. **Phase 6:** `CryptoKitLoader.loadModel` real path, `AuthService.signIn`,
   the TTT model catalog + upload endpoints
9. **Phase 7:** SwiftUI views, app icon, App Store submission per
   `app_store_plan.md`

## What's NOT pre-written

- SwiftUI views (`GamesListView`, `GameDetailView`, etc.) — SwiftUI
  iterates fast in Xcode preview, so stubs add little value vs designing
  in-tool
- The smaller service stubs (e.g. `ModelCatalog`, `TokenStore`, video
  encoder) — straight wrappers around system APIs, faster to write fresh
- Tests — included as design-doc sketches under `swift_*.md` "Parity
  tests" sections; flesh out alongside the implementation
