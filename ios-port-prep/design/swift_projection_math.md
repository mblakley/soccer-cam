# Swift projection math — port spec

Line-by-line port of `video_grouper/inference/cylindrical_view.py` to Swift.
Pure-math module; Accelerate + simd. NO Apple-framework dependencies beyond
`Foundation` — the math is unit-testable on its own.

**Parity target:** given identical inputs, every Swift function returns
`(map_x, map_y)` arrays whose per-pixel difference vs the Python baseline
is < 0.01 px (E0.B1). This is pure double-precision math with no excuse
for drift larger than ULP-level rounding.

## File: `Pipeline/CylindricalView.swift`

### `CylindricalViewParams` struct

Mirror the Python `@dataclass(frozen=True)` field-by-field. Use `-1.0`
sentinel for "derive" on `srcVFovDeg` / `viewVFovDeg`, same as Python.

```swift
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
}

internal func resolvedSrcVFov(_ p: CylindricalViewParams) -> Double {
    p.srcVFovDeg < 0 ? p.srcHFovDeg * Double(p.srcH) / Double(p.srcW) : p.srcVFovDeg
}

internal func resolvedViewVFov(_ p: CylindricalViewParams) -> Double {
    p.viewVFovDeg < 0 ? p.viewHFovDeg * Double(p.outH) / Double(p.outW) : p.viewVFovDeg
}
```

### Pinhole-rays cache

Python uses `@lru_cache(maxsize=8)` keyed on `(outW, outH, hfov, vfov)`. Swift
uses an `NSCache<NSString, PinholeRayBuffers>` or, simpler, a manual dict
guarded by a serial queue. The arrays returned are `[Float]` (the Python
float32 — `_pinhole_rays` switches to float32 to halve trig cost; keep
float32 in Swift for the same reason and the parity bound easily covers it).

```swift
internal struct PinholeRayBuffers {
    let x: [Float]   // length outW * outH, row-major
    let y: [Float]
    let z: [Float]
}

internal enum PinholeRayCache {
    private static let lock = NSLock()
    private static var cache: [String: PinholeRayBuffers] = [:]
    private static let capacity = 8

    static func get(outW: Int, outH: Int, hfov: Double, vfov: Double)
        -> PinholeRayBuffers
    {
        let key = "\(outW)|\(outH)|\(hfov)|\(vfov)"
        lock.lock(); defer { lock.unlock() }
        if let hit = cache[key] { return hit }
        let buffers = computePinholeRays(outW: outW, outH: outH, hfov: hfov, vfov: vfov)
        if cache.count >= capacity { cache.removeAll() }
        cache[key] = buffers
        return buffers
    }
}
```

### `cylindricalRemap`

Returns the per-frame `(mapX, mapY)` arrays for `cv2.remap` equivalent
(Metal warp shader consumes them). Mirror Python's `cylindrical_remap`
exactly. Use `vDSP` for the elementwise trig; or a single sequential loop
for simplicity given Phase 0 says CPU-only is fine on iPhone 14+.

```swift
public func cylindricalRemap(
    _ params: CylindricalViewParams,
    viewYawDeg: Double
) -> (mapX: [Float], mapY: [Float]) {
    let rays = PinholeRayCache.get(
        outW: params.outW,
        outH: params.outH,
        hfov: params.viewHFovDeg,
        vfov: resolvedViewVFov(params)
    )
    // 1) lift (yaw, pitch_offset) from camera frame to world via Rx(-tilt)
    let (xR, yR, zR) = rotateView(
        rays: rays,
        viewYawRad: viewYawDeg.degreesAsRadians,
        viewPitchRad: (params.viewPitchDeg + params.viewPitchOffsetDeg).degreesAsRadians,
        mountTiltRad: params.mountTiltDeg.degreesAsRadians,
        viewRollRad: params.viewRollDeg.degreesAsRadians
    )
    // 2) world rays → camera-frame az/el → equirectangular pixel coords
    return projectToSource(
        xR: xR, yR: yR, zR: zR,
        srcW: params.srcW, srcH: params.srcH,
        srcHFovDeg: params.srcHFovDeg,
        srcVFovDeg: resolvedSrcVFov(params)
    )
}
```

### Functions to port (1:1)

| Python function | Swift equivalent | Notes |
|------|------|------|
| `_resolved_src_vfov(p)` | `resolvedSrcVFov(_:)` | inline trivial |
| `_resolved_view_vfov(p)` | `resolvedViewVFov(_:)` | inline trivial |
| `_pinhole_rays(...)` | `computePinholeRays(...)` | cache via `PinholeRayCache` |
| `_vec(az, el)` | `unitVector(azRad:elRad:)` | scalar; for `view_yaw_pitch_to_pixel` etc. |
| `_rx(a, x, y, z)` | `rotateX(angle:x:y:z:)` | scalar AND array overloads |
| `_ry(a, x, y, z)` | `rotateY(angle:x:y:z:)` | ditto |
| `_rz(a, x, y, z)` | `rotateZ(angle:x:y:z:)` | ditto |
| `_project(...)` | `projectToSource(...)` | the hot loop |
| `_project_numba(...)` | (none needed) | iOS uses plain Swift; CPU is fast enough |
| `cylindrical_remap(p, view_yaw)` | `cylindricalRemap(_:viewYawDeg:)` | public entry point |
| `pixel_to_yaw_pitch(px, py, ...)` | `pixelToYawPitch(...)` | scalar; used by render |
| `yaw_pitch_to_pixel(yaw, pitch, ...)` | `yawPitchToPixel(...)` | scalar; inverse |
| `mount_tilt_from_up(world_up)` | `mountTiltFromUp(_:)` | scalar |
| `leveling_roll(world_up, view_yaw_deg)` | `levelingRoll(worldUp:viewYawDeg:)` | scalar |
| `field_world_up(polygon, ...)` | `fieldWorldUp(polygon:srcW:srcH:srcHFovDeg:)` | uses SVD; `LinearAlgebra` (Accelerate) |
| `crop_box(leveled_pano, params, view_yaw)` | `cropBox(leveledPano:params:viewYawDeg:)` | int math |
| `warp_crop_maps(leveled_pano, params, view_yaw)` | `warpCropMaps(...)` | crop the constant pano arrays |
| `build_leveled_pano(world_up, polygon, ...)` | `buildLeveledPano(...)` | one-shot per game |
| `center_column_rows(p, view_yaw_deg)` | `centerColumnRows(_:viewYawDeg:)` | tight version of cylindrical_remap |
| `LeveledPano` dataclass | `public struct LeveledPano { let mapX: [Float]; let mapY: [Float]; ... }` | |

### Float precision policy

- Pinhole rays + per-frame map arrays: **Float** (matches Python's
  `float32`). The 0.01 px parity bound easily covers float32 round-trip.
- `field_world_up` SVD: **Double** (matches Python's `float64` for the
  3×3 linear algebra). Use `LAPACKE_dgesvd` via Accelerate.
- All scalars in `mount_tilt_from_up`, `leveling_roll`, `pixel_to_yaw_pitch`:
  **Double**.

### simd vs Accelerate vs scalar

- Scalar functions (`pixelToYawPitch`, `yawPitchToPixel`, etc.) — plain
  Swift `Double` math.
- Per-pixel array kernels (`projectToSource`, `computePinholeRays`) — start
  with a plain sequential `for` loop. If iPhone 13 throughput in E0.B1 ends
  up below the 60 fps target, then rewrite with `vDSP_vatan2f` /
  `vDSP_vsincos` etc. Don't pre-optimize.
- 3×3 SVD in `fieldWorldUp` — Accelerate's `sgesvd_` / `dgesvd_`.

### Parity tests (PipelineTests/CylindricalViewTests.swift)

```swift
final class CylindricalViewTests: XCTestCase {
    /// E0.B1: per-pixel diff < 0.01 px vs Python baseline.
    func testCylindricalRemapParity() throws {
        for testCase in TestFixtures.E0_B1_cases() {
            let (mapX, mapY) = cylindricalRemap(testCase.params, viewYawDeg: testCase.viewYaw)
            let baseline = try TestFixtures.loadNpyPair(
                clip: testCase.clip, frame: testCase.frame
            )
            XCTAssertLessThan(maxDiff(mapX, baseline.mapX), 0.01)
            XCTAssertLessThan(maxDiff(mapY, baseline.mapY), 0.01)
        }
    }

    /// E0.B2: leveled-pano build matches Python.
    func testBuildLeveledPano() throws { ... }
}
```

## Cross-references

- `swift_camera_state_machine.md` — calls `pixelToYawPitch`,
  `centerColumnRows`, `cropBox`, `warpCropMaps`
- `metal_warp_shader.md` — consumes the `(mapX, mapY)` produced here
- `data_model.md#world_up_pano` — the leveled-pano parameters persisted in
  carry-over so we only `buildLeveledPano` once per game
