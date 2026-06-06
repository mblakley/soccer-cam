// Pipeline/CylindricalView.swift
//
// Swift port of video_grouper/inference/cylindrical_view.py (soccer-cam,
// feat/broadcast-camera-render). Pure math; depends only on Foundation +
// Accelerate (no AVFoundation / CoreML / Metal — keeps the module unit-
// testable on macOS host alongside the iPhone target).
//
// Parity gate (E0.B1): (mapX, mapY) per-pixel diff < 0.01 px vs the .npy
// baseline at ios-port-prep/baselines/E0.B1/.
//
// See ios-port-prep/design/swift_projection_math.md for the function-by-
// function mapping table.

import Foundation
import Accelerate

// MARK: - CylindricalViewParams

/// Geometry inputs that fully determine the remap grid for one view.
/// `srcVFovDeg` / `viewVFovDeg` may be -1 to mean "derive from aspect ratio".
/// Parity with @dataclass(frozen=True) CylindricalViewParams in
/// cylindrical_view.py.
public struct CylindricalViewParams: Hashable {
    public let srcW: Int
    public let srcH: Int
    public let srcHFovDeg: Double
    public let outW: Int
    public let outH: Int
    public let viewHFovDeg: Double
    public var srcVFovDeg: Double = -1.0
    public var viewVFovDeg: Double = -1.0
    public var viewPitchDeg: Double = 0.0
    public var mountTiltDeg: Double = 0.0
    public var viewPitchOffsetDeg: Double = 0.0
    public var viewRollDeg: Double = 0.0

    public init(
        srcW: Int, srcH: Int, srcHFovDeg: Double,
        outW: Int, outH: Int, viewHFovDeg: Double,
        srcVFovDeg: Double = -1.0, viewVFovDeg: Double = -1.0,
        viewPitchDeg: Double = 0.0, mountTiltDeg: Double = 0.0,
        viewPitchOffsetDeg: Double = 0.0, viewRollDeg: Double = 0.0
    ) {
        self.srcW = srcW; self.srcH = srcH; self.srcHFovDeg = srcHFovDeg
        self.outW = outW; self.outH = outH; self.viewHFovDeg = viewHFovDeg
        self.srcVFovDeg = srcVFovDeg; self.viewVFovDeg = viewVFovDeg
        self.viewPitchDeg = viewPitchDeg; self.mountTiltDeg = mountTiltDeg
        self.viewPitchOffsetDeg = viewPitchOffsetDeg; self.viewRollDeg = viewRollDeg
    }
}

internal func resolvedSrcVFov(_ p: CylindricalViewParams) -> Double {
    p.srcVFovDeg < 0 ? p.srcHFovDeg * Double(p.srcH) / Double(p.srcW) : p.srcVFovDeg
}

internal func resolvedViewVFov(_ p: CylindricalViewParams) -> Double {
    p.viewVFovDeg < 0 ? p.viewHFovDeg * Double(p.outH) / Double(p.outW) : p.viewVFovDeg
}

// MARK: - Pinhole rays cache (parity with @lru_cache _pinhole_rays)

internal struct PinholeRayBuffers {
    let x: [Float]    // length outW * outH, row-major
    let y: [Float]
    let z: [Float]
}

internal enum PinholeRayCache {
    private static let lock = NSLock()
    private static var cache: [String: PinholeRayBuffers] = [:]
    private static let capacity = 8

    static func get(outW: Int, outH: Int, hfovDeg: Double, vfovDeg: Double)
        -> PinholeRayBuffers
    {
        let key = "\(outW)|\(outH)|\(hfovDeg)|\(vfovDeg)"
        lock.lock(); defer { lock.unlock() }
        if let hit = cache[key] { return hit }
        let buffers = computePinholeRays(
            outW: outW, outH: outH, hfovDeg: hfovDeg, vfovDeg: vfovDeg
        )
        if cache.count >= capacity { cache.removeAll() }
        cache[key] = buffers
        return buffers
    }
}

internal func computePinholeRays(
    outW: Int, outH: Int, hfovDeg: Double, vfovDeg: Double
) -> PinholeRayBuffers {
    // TODO: port _pinhole_rays in cylindrical_view.py exactly.
    //   fx = (outW / 2) / tan(hfov/2);   fy = (outH / 2) / tan(vfov/2)
    //   x = (ox - outW/2) / fx;          y = (oy - outH/2) / fy;  z = 1
    //   normalize each ray; return as float32 arrays.
    // Keep float32 — Python uses float32 here too; the trig dominates cost.
    return PinholeRayBuffers(x: [], y: [], z: [])
}

// MARK: - Public entry points

/// Per-frame (mapX, mapY) for the equivalent of cv2.remap. Port of
/// cylindrical_remap(p, view_yaw_deg) in cylindrical_view.py.
public func cylindricalRemap(
    _ params: CylindricalViewParams,
    viewYawDeg: Double
) -> (mapX: [Float], mapY: [Float]) {
    // TODO:
    //   1) get cached pinhole rays for (outW, outH, hfov, vfov)
    //   2) compose rotation: lift view (yaw, pitch) from camera to world via
    //      Rx(-tilt); orient: Ry(yaw_w) · Rx(pitch_w) · Rz(roll) · rayDir
    //   3) map world rays back to camera via Rx(tilt), convert to az/el
    //   4) src_x = (az/srcHFov + 0.5) * srcW; src_y = (el/srcVFov + 0.5) * srcH
    return ([], [])
}

/// One-column-of-the-source equivalent — cheap variant for vertical framing
/// queries in _solve_framing. Parity with center_column_rows(p, view_yaw_deg).
public func centerColumnRows(_ params: CylindricalViewParams, viewYawDeg: Double)
    -> [Float]
{
    // TODO
    return []
}

// MARK: - Scalar utilities (port of _vec, _rx, _ry, _rz, pixel_to_yaw_pitch, etc.)

internal func unitVector(azRad: Double, elRad: Double) -> (Double, Double, Double) {
    let ce = Foundation.cos(elRad)
    let se = Foundation.sin(elRad)
    return (ce * Foundation.sin(azRad), se, ce * Foundation.cos(azRad))
}

internal func rotateX(angleRad: Double,
                      x: Double, y: Double, z: Double) -> (Double, Double, Double) {
    let c = Foundation.cos(angleRad), s = Foundation.sin(angleRad)
    return (x, c * y - s * z, s * y + c * z)
}

internal func rotateY(angleRad: Double,
                      x: Double, y: Double, z: Double) -> (Double, Double, Double) {
    let c = Foundation.cos(angleRad), s = Foundation.sin(angleRad)
    return (c * x + s * z, y, -s * x + c * z)
}

internal func rotateZ(angleRad: Double,
                      x: Double, y: Double, z: Double) -> (Double, Double, Double) {
    let c = Foundation.cos(angleRad), s = Foundation.sin(angleRad)
    return (c * x - s * y, s * x + c * y, z)
}

/// (yaw_deg, pitch_deg) for a source-pixel (px, py). Inverse of yawPitchToPixel.
/// Parity with pixel_to_yaw_pitch in cylindrical_view.py.
public func pixelToYawPitch(
    px: Double, py: Double,
    srcW: Int, srcH: Int, srcHFovDeg: Double
) -> (yawDeg: Double, pitchDeg: Double) {
    // TODO: equirectangular: az = (px/srcW - 0.5) * srcHFov;  el = (py/srcH - 0.5) * srcVFov
    return (0, 0)
}

public func yawPitchToPixel(
    yawDeg: Double, pitchDeg: Double,
    srcW: Int, srcH: Int, srcHFovDeg: Double
) -> (px: Double, py: Double) {
    // TODO
    return (0, 0)
}

// MARK: - World-up / leveling

/// Field polygon → world-up unit vector (a normal to the field plane in
/// camera coords). Uses SVD. Parity with field_world_up.
public func fieldWorldUp(
    polygon: [(Double, Double)],
    srcW: Int, srcH: Int, srcHFovDeg: Double
) -> SIMD3<Double>? {
    // TODO: lift polygon points to unit rays, solve for plane normal via
    // 3×3 SVD on Σ rays·raysᵀ (Accelerate LAPACK dgesvd_).
    return nil
}

public func mountTiltFromUp(_ worldUp: SIMD3<Double>) -> Double {
    // TODO: parity with mount_tilt_from_up
    return 0
}

public func levelingRoll(worldUp: SIMD3<Double>, viewYawDeg: Double) -> Double {
    // TODO: parity with leveling_roll
    return 0
}

// MARK: - Leveled panorama (constant per game)

public struct LeveledPano: Sendable {
    public let mapX: [Float]
    public let mapY: [Float]
    public let width: Int
    public let height: Int
}

public func buildLeveledPano(
    worldUp: SIMD3<Double>,
    polygon: [(Double, Double)],
    srcW: Int, srcH: Int,
    srcHFovDeg: Double, srcVFovDeg: Double
) -> LeveledPano {
    // TODO: parity with build_leveled_pano. ONE per game; reused for every frame.
    return LeveledPano(mapX: [], mapY: [], width: 0, height: 0)
}

public func cropBox(
    leveledPano: LeveledPano,
    params: CylindricalViewParams,
    viewYawDeg: Double
) -> (x: Int, y: Int, w: Int, h: Int) {
    // TODO: parity with crop_box
    return (0, 0, 0, 0)
}

public func warpCropMaps(
    leveledPano: LeveledPano,
    params: CylindricalViewParams,
    viewYawDeg: Double
) -> (mapX: [Float], mapY: [Float]) {
    // TODO: parity with warp_crop_maps — crop the constant pano arrays.
    return ([], [])
}
