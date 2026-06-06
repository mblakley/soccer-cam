// Pipeline/BallTracker.swift
//
// Swift port of video_grouper/inference/ball_tracker.py (soccer-cam,
// feat/broadcast-camera-render). Pure NumPy → pure Swift + Accelerate.
//
// Parity gate (Phase 4): given the same detections.json input, produces a
// trajectory.json that is BYTE-IDENTICAL to the Python baseline at
// ios-port-prep/baselines/<clip>/trajectory.json.
//
// See ios-port-prep/design/swift_kalman_tracker.md for the full port spec.

import Foundation
import Accelerate

// MARK: - Constants (mirror module-level globals in ball_tracker.py)

public enum BallTrackerConstants {
    public static let maxMissingFrames: Int = 15        // MAX_MISSING_FRAMES
    public static let gateDistance: Double = 200        // GATE_DISTANCE
    public static let minTrackLength: Int = 3           // MIN_TRACK_LENGTH
    public static let processNoisePos: Double = 5.0     // PROCESS_NOISE_POS
    public static let processNoiseVel: Double = 10.0    // PROCESS_NOISE_VEL
    public static let processNoiseAcc: Double = 20.0    // PROCESS_NOISE_ACC
    public static let measurementNoise: Double = 15.0   // MEASUREMENT_NOISE
}

// MARK: - Detection (parity with @dataclass Detection)

public struct Detection: Codable, Equatable, Sendable {
    public let x: Double
    public let y: Double
    public let confidence: Double
    public let frameIdx: Int

    public init(x: Double, y: Double, confidence: Double, frameIdx: Int) {
        self.x = x
        self.y = y
        self.confidence = confidence
        self.frameIdx = frameIdx
    }

    // Codable keys match the per-frame entries in detections.json
    // (`cx`, `cy`, `conf`, `frame_idx`).
    enum CodingKeys: String, CodingKey {
        case x = "cx"
        case y = "cy"
        case confidence = "conf"
        case frameIdx = "frame_idx"
    }
}

// MARK: - Kalman state (internal)

/// Mean + covariance of the 6-state constant-acceleration Kalman filter.
/// `x` is length-6; `P` is row-major 6×6 (length 36).
/// Parity with _KalmanState dataclass in ball_tracker.py.
internal struct KalmanState {
    var x: [Double]
    var P: [Double]
}

// MARK: - Track

public struct Track {
    public let trackId: Int
    public internal(set) var detections: [Detection] = []
    public internal(set) var predictions: [(frameIdx: Int, x: Double, y: Double)] = []
    public internal(set) var missingFrames: Int = 0
    public internal(set) var active: Bool = true
    internal var state: KalmanState? = nil

    public var length: Int { detections.count }

    public var lastPosition: (x: Double, y: Double) {
        if let s = state { return (s.x[0], s.x[1]) }
        if let d = detections.last { return (d.x, d.y) }
        return (0, 0)
    }

    init(trackId: Int) { self.trackId = trackId }
}

// MARK: - Kalman matrices (precomputed once)

internal enum KalmanMatrices {
    // dt = 1.0
    // F (state transition) — matches _build_matrices(dt=1.0) in ball_tracker.py
    static let F: [Double] = [
        1, 0, 1, 0, 0.5, 0,
        0, 1, 0, 1, 0, 0.5,
        0, 0, 1, 0, 1, 0,
        0, 0, 0, 1, 0, 1,
        0, 0, 0, 0, 1, 0,
        0, 0, 0, 0, 0, 1,
    ]
    // H (measurement) — 2×6 row-major
    static let H: [Double] = [
        1, 0, 0, 0, 0, 0,
        0, 1, 0, 0, 0, 0,
    ]
    // Q (process noise covariance) — diag([Pos², Pos², Vel², Vel², Acc², Acc²])
    static let Q: [Double] = diag6(
        BallTrackerConstants.processNoisePos * BallTrackerConstants.processNoisePos,
        BallTrackerConstants.processNoisePos * BallTrackerConstants.processNoisePos,
        BallTrackerConstants.processNoiseVel * BallTrackerConstants.processNoiseVel,
        BallTrackerConstants.processNoiseVel * BallTrackerConstants.processNoiseVel,
        BallTrackerConstants.processNoiseAcc * BallTrackerConstants.processNoiseAcc,
        BallTrackerConstants.processNoiseAcc * BallTrackerConstants.processNoiseAcc
    )
    // R (measurement noise covariance) — 2×2 row-major
    static let R: [Double] = [
        BallTrackerConstants.measurementNoise * BallTrackerConstants.measurementNoise, 0,
        0, BallTrackerConstants.measurementNoise * BallTrackerConstants.measurementNoise,
    ]
    static let I6: [Double] = [
        1, 0, 0, 0, 0, 0,
        0, 1, 0, 0, 0, 0,
        0, 0, 1, 0, 0, 0,
        0, 0, 0, 1, 0, 0,
        0, 0, 0, 0, 1, 0,
        0, 0, 0, 0, 0, 1,
    ]
}

// Parity with _initial_state(det) in ball_tracker.py — Mac TODO: verify the
// covariance literal matches the Python ((50, 50, 100, 100, 200, 200) squared).
internal func initialKalmanState(_ det: Detection) -> KalmanState {
    KalmanState(
        x: [det.x, det.y, 0, 0, 0, 0],
        P: diag6(2500, 2500, 10000, 10000, 40000, 40000)
    )
}

// MARK: - BallTracker

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

    /// Carry-over constructor: rebuild from CarryoverTrackerState so a new
    /// segment continues tracks from the previous segment without snapping.
    /// Used by Game.beginSegment(_:carryover:) at segment boundaries.
    public convenience init(from carryover: CarryoverTrackerState) {
        self.init()
        // TODO: rehydrate tracks from carryover.activeTracks; restore nextId.
        // See swift_kalman_tracker.md "Carry-over construction" section.
    }

    /// Port of BallTracker.update(self, frame_idx, detections) in ball_tracker.py.
    /// Order-of-operations is load-bearing for the parity gate — see the
    /// step-numbered comments below; do not reorder.
    @discardableResult
    public func update(frameIdx: Int, detections: [Detection]) -> [Track] {
        // 1) Predict every active track. predictions are appended even on a
        //    no-detection frame so the trajectory can fill gaps later.
        // TODO: for ti in tracks.indices where active && state != nil { predict(&state); record (frameIdx, x[0], x[1]) }

        // 2) Build (dist, ti, di) cost list; gate by gateDistance.
        //    Python `costs.sort()` sorts by tuple lex — replicate exactly so
        //    the greedy assignment is reproducible on tie-breaks.
        // TODO

        // 3) Greedy first-come assign: for each (dist, ti, di) in cost order,
        //    if neither track nor det used yet → Kalman update + bookkeeping.
        // TODO

        // 4) Bump missing-frames on unmatched active tracks; deactivate over budget.
        // TODO

        // 5) Spawn new tracks for unmatched detections.
        // TODO

        return tracks.filter { $0.active }
    }

    private func newTrack(_ det: Detection) -> Track {
        var t = Track(trackId: nextId)
        t.detections = [det]
        t.state = initialKalmanState(det)
        nextId += 1
        return t
    }

    /// Return tracks meeting the minimum-length requirement. Parity with
    /// BallTracker.get_tracks.
    public func tracks(minLength: Int? = nil) -> [Track] {
        let m = minLength ?? minTrackLength
        return tracks.filter { $0.length >= m }
    }

    /// Highest-scoring track: length × average confidence. Parity with
    /// BallTracker.get_best_track.
    public func bestTrack() -> Track? {
        let valid = tracks(minLength: minTrackLength)
        return valid.max { scoreTrack($0) < scoreTrack($1) }
    }

    private func scoreTrack(_ t: Track) -> Double {
        let avgConf = t.detections.reduce(0.0) { $0 + $1.confidence }
            / Double(max(t.detections.count, 1))
        return Double(t.length) * avgConf
    }

    /// Serialize active tracks + the ID counter for hand-off to segment N+1.
    /// See data_model.md#carryover_NNN.json#active_tracks.
    public func makeCarryover(producedBySegment id: String, lastFrameIdx: Int)
        -> CarryoverTrackerState
    {
        // TODO: walk self.tracks, emit CarryoverTrackerState.TrackEntry per
        // active+state-present track; nextTrackId = self.nextId.
        return CarryoverTrackerState(
            producedBySegment: id,
            lastFrameIdx: lastFrameIdx,
            activeTracks: [],
            nextTrackId: nextId
        )
    }
}

// MARK: - Trajectory writer

/// Write the per-frame trajectory ([x, y] or null) JSON matching the Python
/// _run_tracking output. JSONEncoder.sortedKeys() guarantees byte-identical
/// re-runs (parity with sort_keys=True in soccer-cam).
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
        for p in t.predictions where p.frameIdx < trajectory.count && trajectory[p.frameIdx] == nil {
            trajectory[p.frameIdx] = [p.x, p.y]
        }
    }
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let data = try encoder.encode(trajectory)
    try data.write(to: url, options: .atomic)
}

// MARK: - Linear algebra helpers (TODO: Pipeline/MatrixHelpers.swift)

internal func diag6(_ a: Double, _ b: Double, _ c: Double,
                    _ d: Double, _ e: Double, _ f: Double) -> [Double] {
    [
        a, 0, 0, 0, 0, 0,
        0, b, 0, 0, 0, 0,
        0, 0, c, 0, 0, 0,
        0, 0, 0, d, 0, 0,
        0, 0, 0, 0, e, 0,
        0, 0, 0, 0, 0, f,
    ]
}

// _predict (port of ball_tracker.py _predict). Use cblas_dgemm via Accelerate
// to match NumPy's float64 IEEE semantics — DO NOT use simd_double*x* types,
// which fuse multiply-adds and drift by ULPs.
internal func predict(_ state: inout KalmanState) {
    // TODO: state.x = F @ state.x;  state.P = F @ state.P @ F.T + Q
}

// _update (port of ball_tracker.py _update).
internal func update(_ state: inout KalmanState, measurement: (x: Double, y: Double)) {
    // TODO:
    //   y = z - H @ x
    //   S = H @ P @ H.T + R                (2×2)
    //   K = P @ H.T @ inv(S)               (6×2)
    //   x = x + K @ y
    //   P = (I - K @ H) @ P
}
