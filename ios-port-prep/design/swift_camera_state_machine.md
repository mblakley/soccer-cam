# Swift camera state machine — port spec

Port of `_tick`, `_frame_view`, `_CameraState`, `CameraMode`,
`BROADCAST_MODE`, `COACH_MODE`, and the helpers (`_classify_zone`,
`_zone_base_zoom`, `_deadball_zone_zoom`, `_normalized_speed`, `_clamp`,
`_solve_framing`, `_point_in_polygon`, `_ball_field_x`, `compute_entries`)
from `video_grouper/pipeline/steps/render.py` to Swift.

This is the **most behavior-sensitive code** in the port — drift in EMA
constants, ordering of operations, or rounding produces visibly different
"camera feel" even when individual functions return correct values.

**Parity target:** given the same synthetic trajectory (9000 frames),
per-frame `(smoothedYaw, smoothedPitch, smoothedZoom, viewHFov)` differ
from the Python baseline by < 0.001 deg on yaw/pitch and < 0.0005 deg on
the zoom HFOV (E0.B4).

## Files

```
Pipeline/
├── CameraMode.swift              # constants + presets
├── CameraStateMachine.swift      # _tick, _frame_view, _CameraState
├── CameraStateHelpers.swift      # _zone_base_zoom, _normalized_speed, etc.
└── TrajectoryEntries.swift       # compute_entries — finite-diff velocity + EMA
```

## CameraMode.swift

```swift
public struct CameraMode {
    public let zoneBoxBoundary: Double
    public let zoneThirdBoundary: Double

    public let zoomBox: Double
    public let zoomThird: Double
    public let zoomMidfield: Double
    public let zoomSpeedBiasMax: Double

    public let maxLeadRoomFraction: Double

    public let panSmoothingMin: Double
    public let panSmoothingMax: Double
    public let zoomSmoothing: Double
    public let pitchSmoothing: Double

    public let deadballBoxZoom: Double
    public let deadballThirdZoom: Double
    public let deadballMidfieldZoom: Double

    public let deadballSpeedThresholdPxPerFrame: Double
    public let deadballFrameCount: Int
    public let maxExpectedSpeedPxPerFrame: Double

    public let missingBallShortFrames: Int
    public let missingBallMediumFrames: Int
    public let missingBallLongZoom: Double

    public let velocityEMA: Double

    // Defaults match Python BROADCAST_MODE exactly.
    public static let broadcast: CameraMode = .init(
        zoneBoxBoundary: 0.10,
        zoneThirdBoundary: 0.33,
        zoomBox: 0.13,
        zoomThird: 0.18,
        zoomMidfield: 0.22,
        zoomSpeedBiasMax: 0.06,
        maxLeadRoomFraction: 0.20,
        panSmoothingMin: 0.04,
        panSmoothingMax: 0.12,
        zoomSmoothing: 0.03,
        pitchSmoothing: 0.05,
        deadballBoxZoom: 0.13,
        deadballThirdZoom: 0.20,
        deadballMidfieldZoom: 0.28,
        deadballSpeedThresholdPxPerFrame: 4.0,
        deadballFrameCount: 15,
        maxExpectedSpeedPxPerFrame: 100.0,
        missingBallShortFrames: 15,
        missingBallMediumFrames: 60,
        missingBallLongZoom: 0.30,
        velocityEMA: 0.3
    )

    // COACH_MODE overrides — copy the Python literal field-by-field.
    public static let coach: CameraMode = .init( ... )
}
```

## CameraStateMachine.swift

```swift
public struct CameraState {
    public var smoothedYawDeg: Double? = nil
    public var smoothedPitchDeg: Double? = nil
    public var smoothedZoomFrac: Double? = nil
    public var stationaryFrames: Int = 0
    public var missingFrames: Int = 0

    public init() {}

    /// Carry-over constructor — restore from the previous segment's end state.
    public init(from carryover: CarryoverCameraState) {
        self.smoothedYawDeg = carryover.smoothedYawDeg
        self.smoothedPitchDeg = carryover.smoothedPitchDeg
        self.smoothedZoomFrac = carryover.smoothedZoomFrac
        self.stationaryFrames = carryover.stationaryFrames
        self.missingFrames = carryover.missingFrames
    }
}

/// Trajectory entry as produced by compute_entries: optional (px, py, vx, vy).
public typealias TrajectoryEntry = (px: Double, py: Double, vx: Double, vy: Double)?

public final class CameraStateMachine {
    public let mode: CameraMode
    public let renderConfig: RenderConfig
    public let geometry: ViewGeometry
    public let yawMin: Double
    public let yawMax: Double
    public let homography: simd_double3x3?

    public private(set) var state: CameraState

    public init(...) { ... }

    /// One frame of the state machine. Returns the (params, viewYaw) the
    /// renderer feeds to Metal warp. Mirrors _frame_view in Python (which
    /// wraps _tick + leveling_roll). Order-of-operations is load-bearing
    /// for parity — see _tick spec below.
    public func tick(entry: TrajectoryEntry, frameIdx: Int)
        -> (params: CylindricalViewParams, viewYawDeg: Double)
    {
        // 1) call _tick equivalent → (yawDeg, pitchDeg, viewHFovDeg)
        let (yawDeg, pitchDeg, viewHFovDeg) = tickInner(entry: entry)
        // 2) leveling roll from polygon world-up if available
        let roll = geometry.worldUp.map {
            levelingRoll(worldUp: $0, viewYawDeg: yawDeg)
        } ?? renderConfig.viewRollDeg
        // 3) build CylindricalViewParams for cropBox / warpCropMaps
        let params = CylindricalViewParams(
            srcW: geometry.srcW,
            srcH: geometry.srcH,
            srcHFovDeg: geometry.srcHFovDeg,
            outW: renderConfig.outputWidth,
            outH: renderConfig.outputHeight,
            viewHFovDeg: viewHFovDeg,
            viewPitchDeg: pitchDeg,
            mountTiltDeg: geometry.mountTiltDeg,
            viewRollDeg: roll
        )
        return (params, yawDeg)
    }

    /// Port of _tick — see render.py for the canonical comments.
    private func tickInner(entry: TrajectoryEntry) -> (Double, Double, Double) {
        let margin = renderConfig.verticalMarginDeg

        func containmentZoom(ballPitch: Double, viewPitch: Double) -> Double {
            let needVFov = 2.0 * (abs(ballPitch - viewPitch) + margin)
            return (needVFov * geometry.aspect) / geometry.srcHFovDeg
        }

        var resolvedEntry = entry
        if let e = entry,
            renderConfig.maskOffField,
            let polygon = geometry.polygon,
            !pointInPolygon(px: e.px, py: e.py, polygon: polygon)
        {
            resolvedEntry = nil
        }

        guard let e = resolvedEntry else {
            return tickMissing()  // ports the "if entry is None" branch verbatim
        }

        state.missingFrames = 0
        let speed = (e.vx * e.vx + e.vy * e.vy).squareRoot()
        let normSpeed = normalizedSpeed(
            vx: e.vx, vy: e.vy, max: mode.maxExpectedSpeedPxPerFrame
        )

        if speed < mode.deadballSpeedThresholdPxPerFrame {
            state.stationaryFrames += 1
        } else {
            state.stationaryFrames = 0
        }
        let isDeadBall = state.stationaryFrames >= mode.deadballFrameCount

        let (yawRaw, ballPitch) = pixelToYawPitch(
            px: e.px, py: e.py,
            srcW: geometry.srcW, srcH: geometry.srcH,
            srcHFovDeg: geometry.srcHFovDeg
        )

        // --- vertical: track ball pitch (clamped) or hold field centre ---
        if renderConfig.verticalTracking {
            if state.smoothedPitchDeg == nil {
                state.smoothedPitchDeg = ballPitch
            } else {
                state.smoothedPitchDeg! += mode.pitchSmoothing
                    * (ballPitch - state.smoothedPitchDeg!)
            }
        } else {
            state.smoothedPitchDeg = geometry.basePitchDeg
        }
        let viewPitch = state.smoothedPitchDeg!

        // --- zoom: zone+speed or distance+velocity, then containment floor ---
        var targetZoom: Double
        if isDeadBall {
            let fieldX = ballFieldX(px: e.px, py: e.py,
                                    srcW: geometry.srcW, homography: homography)
            targetZoom = deadballZoneZoom(zone: classifyZone(fieldX, mode: mode), mode: mode)
        } else if renderConfig.autoZoom {
            let half = geometry.fieldHalfPitchDeg == 0 ? 1.0 : geometry.fieldHalfPitchDeg
            let depth = clamp(
                (ballPitch - (geometry.basePitchDeg - half)) / (2.0 * half), 0.0, 1.0
            )
            let spd = min(speed / renderConfig.zoomSpeedNormPx, 1.0)
            let hfov = clamp(
                renderConfig.zoomBaseDeg
                    + spd * renderConfig.zoomSpeedGainDeg
                    + depth * renderConfig.zoomDepthGainDeg,
                renderConfig.zoomMinDeg, renderConfig.zoomMaxDeg
            )
            targetZoom = hfov / geometry.srcHFovDeg
        } else {
            let fieldX = ballFieldX(px: e.px, py: e.py,
                                    srcW: geometry.srcW, homography: homography)
            targetZoom = zoneBaseZoom(zone: classifyZone(fieldX, mode: mode), mode: mode)
                + normSpeed * mode.zoomSpeedBiasMax
        }
        if renderConfig.verticalTracking {
            targetZoom = max(targetZoom, containmentZoom(ballPitch: ballPitch, viewPitch: viewPitch))
        } else {
            let wholeField = (
                2.0 * (geometry.fieldHalfPitchDeg + margin) * geometry.aspect
            ) / geometry.srcHFovDeg
            targetZoom = max(
                targetZoom, wholeField,
                containmentZoom(ballPitch: ballPitch, viewPitch: viewPitch)
            )
        }

        // --- pan: ball yaw + velocity lead room ---
        var targetYaw: Double
        if speed > 1e-6 {
            let cropWidthPx = targetZoom * Double(geometry.srcW)
            let maxLeadPx = mode.maxLeadRoomFraction * cropWidthPx
            let leadPx = (e.vx / speed) * (normSpeed * maxLeadPx)
            targetYaw = yawRaw + leadPx * (geometry.srcHFovDeg / Double(geometry.srcW))
        } else {
            targetYaw = yawRaw
        }

        let panAlpha = mode.panSmoothingMin
            + (mode.panSmoothingMax - mode.panSmoothingMin) * normSpeed

        if state.smoothedYawDeg == nil {
            state.smoothedYawDeg = targetYaw
        } else {
            state.smoothedYawDeg! += panAlpha * (targetYaw - state.smoothedYawDeg!)
        }
        if state.smoothedZoomFrac == nil {
            state.smoothedZoomFrac = targetZoom
        } else {
            state.smoothedZoomFrac! += mode.zoomSmoothing
                * (targetZoom - state.smoothedZoomFrac!)
        }

        state.smoothedYawDeg = clamp(state.smoothedYawDeg!, yawMin, yawMax)
        // Clamp pitch so the view's vertical extent never samples past the source.
        let viewHFov = state.smoothedZoomFrac! * geometry.srcHFovDeg
        let viewVFov = viewHFov * Double(renderConfig.outputHeight) / Double(renderConfig.outputWidth)
        let pitchRoom = max(0.0, geometry.pitchLimitDeg - viewVFov / 2.0)
        state.smoothedPitchDeg = clamp(state.smoothedPitchDeg!, -pitchRoom, pitchRoom)

        return (state.smoothedYawDeg!, state.smoothedPitchDeg!, viewHFov)
    }

    private func tickMissing() -> (Double, Double, Double) { ... }   // port "if entry is None" branch
}
```

## Helpers (CameraStateHelpers.swift)

```swift
func clamp(_ v: Double, _ lo: Double, _ hi: Double) -> Double { ... }

func normalizedSpeed(vx: Double, vy: Double, max: Double) -> Double {
    let s = (vx * vx + vy * vy).squareRoot()
    return min(s / max, 1.0)
}

func classifyZone(_ fieldX: Double, mode: CameraMode) -> Zone { ... }
func zoneBaseZoom(zone: Zone, mode: CameraMode) -> Double { ... }
func deadballZoneZoom(zone: Zone, mode: CameraMode) -> Double { ... }

func pointInPolygon(px: Double, py: Double, polygon: [(Double, Double)]) -> Bool {
    var inside = false, j = polygon.count - 1
    for i in 0..<polygon.count {
        let (xi, yi) = polygon[i]
        let (xj, yj) = polygon[j]
        if (yi > py) != (yj > py),
            px < (xj - xi) * (py - yi) / (yj - yi) + xi
        {
            inside.toggle()
        }
        j = i
    }
    return inside
}

func ballFieldX(px: Double, py: Double, srcW: Int, homography: simd_double3x3?) -> Double { ... }
```

## TrajectoryEntries.swift

Port of `compute_entries` — applies EMA to per-frame finite-difference
velocity. The Python version returns a list of `Optional[Tuple[float, ...]]`;
Swift returns `[TrajectoryEntry]`.

```swift
public func computeEntries(
    rawTrajectory: [[Double]?],
    velocityEMA: Double
) -> [TrajectoryEntry] { ... }
```

## Parity tests

```swift
final class CameraStateMachineTests: XCTestCase {
    /// E0.B4: per-frame state diverges < 0.001° (yaw/pitch) / 0.0005° (hfov) vs Python.
    func testTickDeterminismOver9000Frames() throws {
        let trajectory = try TestFixtures.syntheticTrajectory9000()  // checked-in
        let baselineStates = try TestFixtures.loadCameraStatesJSON(  // baseline
            "e0_b4_synthetic_baseline.json"
        )
        let machine = CameraStateMachine(
            mode: .broadcast,
            renderConfig: TestFixtures.E0_B4_renderConfig,
            geometry: TestFixtures.E0_B4_geometry,
            yawMin: -85, yawMax: 85,
            homography: nil
        )
        for (idx, entry) in trajectory.enumerated() {
            let (params, viewYaw) = machine.tick(entry: entry, frameIdx: idx)
            let baseline = baselineStates[idx]
            XCTAssertLessThan(abs(viewYaw - baseline.viewYawDeg), 0.001)
            XCTAssertLessThan(abs(params.viewPitchDeg - baseline.viewPitchDeg), 0.001)
            XCTAssertLessThan(abs(params.viewHFovDeg - baseline.viewHFovDeg), 0.0005)
        }
    }

    /// Carry-over: state at end of segment N loaded at start of N+1 produces
    /// the same per-frame trajectory as a continuous run over the concat.
    func testSegmentBoundaryParityViaCarryover() throws { ... }
}
```

## Cross-references

- `swift_projection_math.md` — `CylindricalViewParams`, `pixelToYawPitch`,
  `levelingRoll`, `cropBox`, `warpCropMaps`
- `data_model.md#carryover_NNN.json#camera_state` — what `CameraState`
  serializes to
- `segment_pipeline.md` — drives `tick(entry:frameIdx:)` per frame inside
  `SegmentProcessor`
