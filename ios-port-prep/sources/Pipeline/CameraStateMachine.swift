// Pipeline/CameraStateMachine.swift
//
// Swift port of render.py _tick, _frame_view, _CameraState, CameraMode,
// _solve_framing, _classify_zone, _zone_base_zoom, _deadball_zone_zoom,
// _normalized_speed, _clamp, _point_in_polygon, _ball_field_x, compute_entries.
//
// This is the MOST BEHAVIOR-SENSITIVE port — EMA constants and operation
// ordering must match Python exactly or the camera "feel" drifts visibly.
//
// Parity gate (E0.B4): given a 9000-frame synthetic trajectory, per-frame
// (yaw, pitch, hfov) differ from the Python baseline by < 0.001° / 0.0005°.
//
// See ios-port-prep/design/swift_camera_state_machine.md for the function
// mapping.

import Foundation
import simd

// MARK: - CameraMode (parity with @dataclass(frozen=True) CameraMode)

public struct CameraMode: Sendable {
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

    // Defaults match Python BROADCAST_MODE exactly. (See render.py around line ~165.)
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

    // TODO: COACH_MODE overrides — copy field-by-field from render.py.
    public static let coach: CameraMode = .broadcast
}

public enum FieldZone: Sendable { case box, third, midfield }

// MARK: - RenderConfig (subset of RenderStepConfig the state machine needs)

public struct RenderConfig: Sendable {
    public var outputWidth: Int = 1920
    public var outputHeight: Int = 1080
    public var srcHFovDeg: Double = 180.0
    public var verticalTracking: Bool = true
    public var verticalMarginDeg: Double = 6.0
    public var autoZoom: Bool = true
    public var zoomBaseDeg: Double = 47.0
    public var zoomMinDeg: Double = 46.0
    public var zoomMaxDeg: Double = 58.0
    public var zoomSpeedNormPx: Double = 15.0
    public var zoomSpeedGainDeg: Double = 8.0
    public var zoomDepthGainDeg: Double = 5.0
    public var maskOffField: Bool = true
    public var viewRollDeg: Double = 0.0
    public var mountTiltDeg: Double = 0.0
}

// MARK: - ViewGeometry (resolved per game, equiv of _ViewGeom in render.py)

public struct ViewGeometry: Sendable {
    public let srcW: Int
    public let srcH: Int
    public let srcHFovDeg: Double
    public let srcVFovDeg: Double
    public let aspect: Double
    public let basePitchDeg: Double
    public let fieldHalfPitchDeg: Double
    public let pitchLimitDeg: Double
    public let mountTiltDeg: Double
    public let worldUp: SIMD3<Double>?
    public let polygon: [(Double, Double)]?
}

// MARK: - CameraState (parity with _CameraState)

public struct CameraState: Sendable {
    public var smoothedYawDeg: Double? = nil
    public var smoothedPitchDeg: Double? = nil
    public var smoothedZoomFrac: Double? = nil
    public var stationaryFrames: Int = 0
    public var missingFrames: Int = 0

    public init() {}

    public init(from carryover: CarryoverCameraState) {
        self.smoothedYawDeg = carryover.smoothedYawDeg
        self.smoothedPitchDeg = carryover.smoothedPitchDeg
        self.smoothedZoomFrac = carryover.smoothedZoomFrac
        self.stationaryFrames = carryover.stationaryFrames
        self.missingFrames = carryover.missingFrames
    }
}

// MARK: - Trajectory entry (output of compute_entries)

/// Per-frame trajectory entry as produced by compute_entries: (px, py, vx, vy).
/// nil when no detection / no tracker output for that frame.
public typealias TrajectoryEntry = (px: Double, py: Double, vx: Double, vy: Double)?

// MARK: - CameraStateMachine

public final class CameraStateMachine {
    public let mode: CameraMode
    public let renderConfig: RenderConfig
    public let geometry: ViewGeometry
    public let yawMinDeg: Double
    public let yawMaxDeg: Double
    public let homography: simd_double3x3?

    public private(set) var state: CameraState
    public private(set) var lastFrameIdx: Int = -1

    public init(
        mode: CameraMode,
        renderConfig: RenderConfig,
        geometry: ViewGeometry,
        yawMinDeg: Double,
        yawMaxDeg: Double,
        homography: simd_double3x3?,
        initialState: CameraState = CameraState()
    ) {
        self.mode = mode
        self.renderConfig = renderConfig
        self.geometry = geometry
        self.yawMinDeg = yawMinDeg
        self.yawMaxDeg = yawMaxDeg
        self.homography = homography
        self.state = initialState
    }

    /// Per-frame step. Port of render.py _frame_view (which wraps _tick +
    /// leveling_roll + CylindricalViewParams build). Returns the (params,
    /// viewYawDeg) the renderer feeds to Metal warp.
    public func tick(entry: TrajectoryEntry, frameIdx: Int)
        -> (params: CylindricalViewParams, viewYawDeg: Double)
    {
        lastFrameIdx = frameIdx

        // 1) Run the inner _tick equivalent. Order-of-operations matters; see
        //    render.py lines ~517-680 for the canonical comments.
        let (yawDeg, pitchDeg, viewHFovDeg) = tickInner(entry: entry)

        // 2) Polygon-derived leveling roll (else fall back to renderConfig).
        let rollDeg: Double
        if let worldUp = geometry.worldUp {
            rollDeg = levelingRoll(worldUp: worldUp, viewYawDeg: yawDeg)
        } else {
            rollDeg = renderConfig.viewRollDeg
        }

        // 3) Build the cylindrical params for the renderer.
        let params = CylindricalViewParams(
            srcW: geometry.srcW,
            srcH: geometry.srcH,
            srcHFovDeg: geometry.srcHFovDeg,
            outW: renderConfig.outputWidth,
            outH: renderConfig.outputHeight,
            viewHFovDeg: viewHFovDeg,
            viewPitchDeg: pitchDeg,
            mountTiltDeg: geometry.mountTiltDeg,
            viewRollDeg: rollDeg
        )
        return (params, yawDeg)
    }

    /// Port of _tick. The branches are intentionally verbose / sequential to
    /// mirror Python and make divergence easy to spot on a diff.
    private func tickInner(entry: TrajectoryEntry) -> (Double, Double, Double) {
        // Off-field rejection: matches the `if entry is not None and ... and not
        // _point_in_polygon(...)` block early in _tick. Treat off-field as missing.
        var resolvedEntry = entry
        if let e = entry,
           renderConfig.maskOffField,
           let polygon = geometry.polygon,
           !pointInPolygon(px: e.px, py: e.py, polygon: polygon)
        {
            resolvedEntry = nil
        }

        guard let e = resolvedEntry else {
            return tickMissing()
        }

        // --- entry present branch ---
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

        // --- vertical: track ball pitch (smoothed) or hold field centre ---
        // TODO: port the `if cfg.render_vertical_tracking` branch verbatim
        //       (render.py around line 595-605).

        // --- zoom: zone+speed OR distance+velocity, then containment floor ---
        // TODO: port the `if is_dead_ball / elif cfg.render_auto_zoom / else`
        //       branches (render.py 607-645).
        let targetZoom: Double = mode.zoomMidfield   // placeholder

        // --- pan: ball yaw + velocity lead room ---
        // TODO: port the speed > 1e-6 branch (render.py 647-654).
        let targetYaw = yawRaw

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

        state.smoothedYawDeg = clamp(state.smoothedYawDeg!, yawMinDeg, yawMaxDeg)
        let viewHFov = state.smoothedZoomFrac! * geometry.srcHFovDeg
        let viewVFov = viewHFov * Double(renderConfig.outputHeight)
            / Double(renderConfig.outputWidth)
        let pitchRoom = max(0.0, geometry.pitchLimitDeg - viewVFov / 2.0)
        state.smoothedPitchDeg = clamp(
            state.smoothedPitchDeg ?? geometry.basePitchDeg,
            -pitchRoom, pitchRoom
        )

        // ballPitch is read by the vertical-tracking branch above; silenced here
        // until that branch is filled in.
        _ = ballPitch

        return (state.smoothedYawDeg!, state.smoothedPitchDeg!, viewHFov)
    }

    /// Port of the `if entry is None` branch in _tick.
    private func tickMissing() -> (Double, Double, Double) {
        // TODO: port render.py lines 549-580 verbatim.
        let yaw = state.smoothedYawDeg ?? 0
        let pitch = state.smoothedPitchDeg ?? geometry.basePitchDeg
        let zoom = state.smoothedZoomFrac ?? mode.zoomMidfield
        return (yaw, pitch, zoom * geometry.srcHFovDeg)
    }
}

// MARK: - Helpers (CameraStateHelpers.swift in W.6 split — inlined here for now)

internal func clamp(_ v: Double, _ lo: Double, _ hi: Double) -> Double {
    min(max(v, lo), hi)
}

internal func normalizedSpeed(vx: Double, vy: Double, max maxValue: Double) -> Double {
    let s = (vx * vx + vy * vy).squareRoot()
    return Swift.min(s / maxValue, 1.0)
}

internal func classifyZone(_ fieldX: Double, mode: CameraMode) -> FieldZone {
    let absX = abs(fieldX - 0.5) * 2.0
    if absX < mode.zoneBoxBoundary { return .box }
    if absX < mode.zoneThirdBoundary { return .third }
    return .midfield
}

internal func zoneBaseZoom(zone: FieldZone, mode: CameraMode) -> Double {
    switch zone {
    case .box: return mode.zoomBox
    case .third: return mode.zoomThird
    case .midfield: return mode.zoomMidfield
    }
}

internal func deadballZoneZoom(zone: FieldZone, mode: CameraMode) -> Double {
    switch zone {
    case .box: return mode.deadballBoxZoom
    case .third: return mode.deadballThirdZoom
    case .midfield: return mode.deadballMidfieldZoom
    }
}

internal func pointInPolygon(px: Double, py: Double, polygon: [(Double, Double)]) -> Bool {
    var inside = false
    var j = polygon.count - 1
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

internal func ballFieldX(
    px: Double, py: Double, srcW: Int, homography: simd_double3x3?
) -> Double {
    // TODO: parity with _ball_field_x — apply homography, normalize 0..1.
    return Double(px) / Double(srcW)
}

// MARK: - compute_entries

/// Apply EMA to finite-difference velocity to produce TrajectoryEntry per frame.
/// Port of compute_entries in render.py (around line ~?). Returns one entry
/// per frame in the input trajectory; nil for null positions.
public func computeEntries(
    rawTrajectory: [[Double]?],
    velocityEMA: Double
) -> [TrajectoryEntry] {
    // TODO: walk the trajectory, for each populated frame compute finite-
    // diff velocity vs previous populated frame, apply EMA smoothing.
    return Array(repeating: nil, count: rawTrajectory.count)
}
