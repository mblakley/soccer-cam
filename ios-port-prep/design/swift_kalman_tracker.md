# Swift Kalman Tracker — port spec

Line-by-line port of `video_grouper/inference/ball_tracker.py` to Swift.
The Python tracker is pure NumPy (no scipy/filterpy/ByteTrack), so this is
the easiest port in the whole project — and the strictest parity gate.

**Parity target:** given the same `detections.json`, the Swift tracker
produces a `trajectory.json` that is **byte-identical** to the Python
baseline at `ios-port-prep/baselines/<clip>/trajectory.json`. This is the
load-bearing assumption of every downstream phase, so the parity check
must hold with no tolerance.

## File: `Pipeline/BallTracker.swift`

```swift
import Foundation
import Accelerate

// Constants — mirror module-level globals in ball_tracker.py exactly.
public enum BallTrackerConstants {
    public static let maxMissingFrames: Int = 15
    public static let gateDistance: Double = 200
    public static let minTrackLength: Int = 3
    public static let processNoisePos: Double = 5.0
    public static let processNoiseVel: Double = 10.0
    public static let processNoiseAcc: Double = 20.0
    public static let measurementNoise: Double = 15.0
}

public struct Detection: Codable, Equatable {
    public let x: Double
    public let y: Double
    public let confidence: Double
    public let frameIdx: Int

    // Codable field-name parity with the Python schema's per-frame entries.
    enum CodingKeys: String, CodingKey {
        case x = "cx"
        case y = "cy"
        case confidence = "conf"
        case frameIdx = "frame_idx"
    }
}

// 6-state Kalman: [x, y, vx, vy, ax, ay]
internal struct KalmanState {
    var x: [Double]   // length 6
    var P: [Double]   // length 36, row-major 6×6
}

public struct Track {
    public let trackId: Int
    public internal(set) var detections: [Detection] = []
    public internal(set) var predictions: [(frameIdx: Int, x: Double, y: Double)] = []
    public internal(set) var missingFrames: Int = 0
    public internal(set) var active: Bool = true
    internal var state: KalmanState? = nil

    public var length: Int { detections.count }

    public var lastPosition: (x: Double, y: Double) {
        if let s = state {
            return (s.x[0], s.x[1])
        }
        if let d = detections.last {
            return (d.x, d.y)
        }
        return (0, 0)
    }
}
```

## Matrices — module-level constants

Mirror the Python `_build_matrices(dt=1.0)` output. Precompute at first use,
stored as immutable globals (the Python module-level `_F`, `_H`, `_Q`, `_R`,
`_I6`).

```swift
internal enum KalmanMatrices {
    // dt = 1.0
    // F = [
    //   [1, 0, 1,  0, 0.5, 0],
    //   [0, 1, 0,  1, 0,   0.5],
    //   [0, 0, 1,  0, 1,   0],
    //   [0, 0, 0,  1, 0,   1],
    //   [0, 0, 0,  0, 1,   0],
    //   [0, 0, 0,  0, 0,   1],
    // ]
    static let F: [Double] = [
        1, 0, 1, 0, 0.5, 0,
        0, 1, 0, 1, 0, 0.5,
        0, 0, 1, 0, 1, 0,
        0, 0, 0, 1, 0, 1,
        0, 0, 0, 0, 1, 0,
        0, 0, 0, 0, 0, 1,
    ]

    // H = [
    //   [1, 0, 0, 0, 0, 0],
    //   [0, 1, 0, 0, 0, 0],
    // ]  shape 2×6
    static let H: [Double] = [
        1, 0, 0, 0, 0, 0,
        0, 1, 0, 0, 0, 0,
    ]

    // Q = diag([Pos², Pos², Vel², Vel², Acc², Acc²])
    static let Q: [Double] = diag6(
        pow(BallTrackerConstants.processNoisePos, 2),
        pow(BallTrackerConstants.processNoisePos, 2),
        pow(BallTrackerConstants.processNoiseVel, 2),
        pow(BallTrackerConstants.processNoiseVel, 2),
        pow(BallTrackerConstants.processNoiseAcc, 2),
        pow(BallTrackerConstants.processNoiseAcc, 2)
    )

    // R = diag([Meas², Meas²])  shape 2×2
    static let R: [Double] = [
        pow(BallTrackerConstants.measurementNoise, 2), 0,
        0, pow(BallTrackerConstants.measurementNoise, 2),
    ]

    static let I6: [Double] = identity6()
}
```

## Initial state

Match `_initial_state(det)`. The initial covariance Python sets is
`diag([50², 50², 100², 100², 200², 200²])` — copy exactly.

```swift
internal func initialState(_ det: Detection) -> KalmanState {
    let x: [Double] = [det.x, det.y, 0, 0, 0, 0]
    let P = diag6(2500, 2500, 10000, 10000, 40000, 40000)
    return KalmanState(x: x, P: P)
}
```

## predict / update

Use Accelerate's `vDSP` + `cblas_dgemm` for the matrix ops to match
NumPy's double-precision IEEE 754 semantics exactly. Don't use `simd_double*`
for these — `simd_*` types fuse multiply-adds in ways that drift by ULP-
level differences from NumPy.

```swift
internal func predict(_ state: inout KalmanState) {
    // x = F @ x
    let newX = matVecMul(KalmanMatrices.F, rows: 6, cols: 6, vec: state.x)
    state.x = newX
    // P = F @ P @ F.T + Q
    let FP = matMul(KalmanMatrices.F, rowsA: 6, colsA: 6, B: state.P, rowsB: 6, colsB: 6)
    let FPFt = matMulT(FP, rowsA: 6, colsA: 6, B: KalmanMatrices.F, rowsB: 6, colsB: 6)
    state.P = elementWiseAdd(FPFt, KalmanMatrices.Q)
}

internal func update(_ state: inout KalmanState, measurement: (x: Double, y: Double)) {
    // innovation = z - H @ x
    let z: [Double] = [measurement.x, measurement.y]
    let Hx = matVecMul(KalmanMatrices.H, rows: 2, cols: 6, vec: state.x)
    let y = [z[0] - Hx[0], z[1] - Hx[1]]
    // S = H @ P @ H.T + R   (2×2)
    let HP = matMul(KalmanMatrices.H, rowsA: 2, colsA: 6, B: state.P, rowsB: 6, colsB: 6)
    let HPHt = matMulT(HP, rowsA: 2, colsA: 6, B: KalmanMatrices.H, rowsB: 2, colsB: 6)
    let S = elementWiseAdd(HPHt, KalmanMatrices.R)
    // K = P @ H.T @ inv(S)   (6×2)
    let Sinv = invert2x2(S)
    let PHt = matMulT(state.P, rowsA: 6, colsA: 6, B: KalmanMatrices.H, rowsB: 2, colsB: 6)
    let K = matMul(PHt, rowsA: 6, colsA: 2, B: Sinv, rowsB: 2, colsB: 2)
    // x = x + K @ y
    let Ky = matVecMul(K, rows: 6, cols: 2, vec: y)
    state.x = zip(state.x, Ky).map(+)
    // P = (I - K @ H) @ P
    let KH = matMul(K, rowsA: 6, colsA: 2, B: KalmanMatrices.H, rowsB: 2, colsB: 6)
    let ImKH = elementWiseSub(KalmanMatrices.I6, KH)
    state.P = matMul(ImKH, rowsA: 6, colsA: 6, B: state.P, rowsB: 6, colsB: 6)
}
```

The matMul / matVecMul / etc. helpers wrap `cblas_dgemm` / `cblas_dgemv` with
row-major flags. See `Pipeline/MatrixHelpers.swift` (W.6 stub).

## BallTracker class

Port of `class BallTracker`. Each method is a 1:1 mapping; order of
operations within `update()` is load-bearing for parity (see comment).

```swift
public final class BallTracker {
    public let gateDistance: Double
    public let maxMissing: Int
    public let minTrackLength: Int

    public private(set) var tracks: [Track] = []
    private var nextId: Int = 0

    public init(
        gateDistance: Double = BallTrackerConstants.gateDistance,
        maxMissing: Int = BallTrackerConstants.maxMissingFrames,
        minTrackLength: Int = BallTrackerConstants.minTrackLength
    ) {
        self.gateDistance = gateDistance
        self.maxMissing = maxMissing
        self.minTrackLength = minTrackLength
    }

    /// Carry-over construction: rebuild from carryover_NNN.json contents
    /// so segment boundaries don't reset the tracker.
    public init(from carryover: CarryoverTrackerState) {
        self.gateDistance = BallTrackerConstants.gateDistance
        self.maxMissing = BallTrackerConstants.maxMissingFrames
        self.minTrackLength = BallTrackerConstants.minTrackLength
        self.tracks = carryover.activeTracks.map { entry in
            var t = Track(trackId: entry.trackId)
            t.state = KalmanState(x: entry.kalmanState.x, P: entry.kalmanState.PFlat)
            t.missingFrames = entry.missingFrames
            return t
        }
        self.nextId = carryover.nextTrackId
    }

    @discardableResult
    public func update(frameIdx: Int, detections: [Detection]) -> [Track] {
        // 1) predict every active track
        for i in tracks.indices where tracks[i].active && tracks[i].state != nil {
            var state = tracks[i].state!
            predict(&state)
            tracks[i].state = state
            tracks[i].predictions.append(
                (frameIdx: frameIdx, x: state.x[0], y: state.x[1])
            )
        }

        // 2) build (dist, trackIdx, detIdx) cost list; gate by gateDistance
        var costs: [(dist: Double, ti: Int, di: Int)] = []
        for (ti, track) in tracks.enumerated() where track.active {
            let (tx, ty) = track.lastPosition
            for (di, det) in detections.enumerated() {
                let dx = det.x - tx, dy = det.y - ty
                let dist = (dx * dx + dy * dy).squareRoot()
                if dist < gateDistance {
                    costs.append((dist, ti, di))
                }
            }
        }
        // Python: costs.sort()  — sorts by tuple, i.e. (dist, ti, di) lex.
        costs.sort { lhs, rhs in
            if lhs.dist != rhs.dist { return lhs.dist < rhs.dist }
            if lhs.ti != rhs.ti { return lhs.ti < rhs.ti }
            return lhs.di < rhs.di
        }

        // 3) greedy assign: first-come per (ti, di), update both Kalman + bookkeeping
        var usedTracks = Set<Int>()
        var usedDets = Set<Int>()
        for entry in costs {
            if usedTracks.contains(entry.ti) || usedDets.contains(entry.di) { continue }
            var state = tracks[entry.ti].state!
            let det = detections[entry.di]
            update(&state, measurement: (det.x, det.y))
            tracks[entry.ti].state = state
            tracks[entry.ti].detections.append(det)
            tracks[entry.ti].missingFrames = 0
            usedTracks.insert(entry.ti)
            usedDets.insert(entry.di)
        }

        // 4) bump missing-frames on unmatched active tracks; deactivate over budget
        for (ti, _) in tracks.enumerated()
        where tracks[ti].active && !usedTracks.contains(ti) {
            tracks[ti].missingFrames += 1
            if tracks[ti].missingFrames > maxMissing {
                tracks[ti].active = false
            }
        }

        // 5) spawn new tracks for unmatched detections
        for (di, det) in detections.enumerated() where !usedDets.contains(di) {
            tracks.append(newTrack(det))
        }

        return tracks.filter { $0.active }
    }

    private func newTrack(_ det: Detection) -> Track {
        var t = Track(trackId: nextId)
        t.detections = [det]
        t.state = initialState(det)
        nextId += 1
        return t
    }

    public func tracks(minLength: Int? = nil) -> [Track] {
        let m = minLength ?? minTrackLength
        return tracks.filter { $0.length >= m }
    }

    public func bestTrack() -> Track? {
        let valid = tracks(minLength: minTrackLength)
        return valid.max { a, b in scoreTrack(a) < scoreTrack(b) }
    }

    private func scoreTrack(_ t: Track) -> Double {
        let avgConf = t.detections.reduce(0.0) { $0 + $1.confidence }
            / Double(max(t.detections.count, 1))
        return Double(t.length) * avgConf
    }

    /// Serialize active tracks + next ID so segment N+1 can resume.
    public func makeCarryover(producedBySegment id: String, lastFrameIdx: Int)
        -> CarryoverTrackerState
    {
        let active = tracks.compactMap { t -> CarryoverTrackerState.TrackEntry? in
            guard t.active, let s = t.state else { return nil }
            return .init(
                trackId: t.trackId,
                kalmanState: .init(x: s.x, PFlat: s.P),
                missingFrames: t.missingFrames,
                lastSeenFrameIdx: t.detections.last?.frameIdx ?? lastFrameIdx
            )
        }
        return CarryoverTrackerState(
            producedBySegment: id,
            lastFrameIdx: lastFrameIdx,
            activeTracks: active,
            nextTrackId: nextId
        )
    }
}
```

## Trajectory writer

Match the Python `_run_tracking` output schema exactly. Path:
`Documents/games/<gameId>/debug/trajectory_<seqNNN>.json` (debug-only) or
in-memory when the renderer is the only consumer.

```swift
public func writeTrajectory(
    bestTrack: Track?,
    lastFrameIdx: Int,
    to url: URL
) throws {
    var trajectory: [[Double]?] = Array(repeating: nil, count: lastFrameIdx + 1)
    if let t = bestTrack {
        for det in t.detections {
            trajectory[det.frameIdx] = [det.x, det.y]
        }
        for p in t.predictions where p.frameIdx < trajectory.count
            && trajectory[p.frameIdx] == nil
        {
            trajectory[p.frameIdx] = [p.x, p.y]
        }
    }
    let data = try JSONEncoder.sortedKeys().encode(trajectory)
    try data.write(to: url, options: .atomic)
}
```

`JSONEncoder.sortedKeys()` returns an encoder with
`outputFormatting = [.sortedKeys]` — required for byte-identical parity
with the Python `sort_keys=True` dump.

## Parity tests (PipelineTests/BallTrackerTests.swift)

```swift
final class BallTrackerTests: XCTestCase {
    /// E0.A1 / Phase 4 parity: byte-identical trajectory.json vs the Python baseline.
    func testTrajectoryByteIdentical() throws {
        for clipName in TestFixtures.goldenClips {
            let detections = try TestFixtures.loadDetections(clip: clipName)
            let baseline = try TestFixtures.loadTrajectoryBytes(clip: clipName)

            let tracker = BallTracker()
            for (frameIdx, frameDets) in groupByFrame(detections) {
                tracker.update(frameIdx: frameIdx, detections: frameDets)
            }

            let out = URL.temporary("trajectory.json")
            try writeTrajectory(
                bestTrack: tracker.bestTrack(),
                lastFrameIdx: detections.map(\.frameIdx).max() ?? 0,
                to: out
            )
            let actual = try Data(contentsOf: out)
            XCTAssertEqual(actual, baseline, "trajectory diverged for \(clipName)")
        }
    }

    /// Carry-over round-trip: encode + decode produces functionally identical tracker.
    func testCarryoverRoundTrip() throws { ... }
}
```

## Cross-references

- `data_model.md#carryover_NNN.json` — schema for the carry-over file
- `swift_camera_state_machine.md` — consumes the tracker's per-frame
  trajectory output via `compute_entries`
- `segment_pipeline.md` — calls `BallTracker.update` per frame, then
  `bestTrack` / `writeTrajectory` at end of segment
