// Domain/CarryoverState.swift
//
// Per-segment carry-over state passed from segment N to segment N+1 so the
// tracker and camera state machine don't reset at every boundary.
//
// Schema parity with ios-port-prep/design/data_model.md#carryover_NNN.json.

import Foundation

public struct CarryoverState: Codable, Sendable {
    public let schemaVersion: Int
    public let producedBySegment: String
    public let producedAt: Date
    public let lastFrameIdx: Int

    public let trackerState: CarryoverTrackerState
    public let cameraState: CarryoverCameraState
    public let worldUpPano: CarryoverWorldUpPano?

    public init(
        schemaVersion: Int = 1,
        producedBySegment: String,
        producedAt: Date,
        lastFrameIdx: Int,
        trackerState: CarryoverTrackerState,
        cameraState: CarryoverCameraState,
        worldUpPano: CarryoverWorldUpPano?
    ) {
        self.schemaVersion = schemaVersion
        self.producedBySegment = producedBySegment
        self.producedAt = producedAt
        self.lastFrameIdx = lastFrameIdx
        self.trackerState = trackerState
        self.cameraState = cameraState
        self.worldUpPano = worldUpPano
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case producedBySegment = "produced_by_segment"
        case producedAt = "produced_at"
        case lastFrameIdx = "last_frame_idx"
        case trackerState = "tracker_state"
        case cameraState = "camera_state"
        case worldUpPano = "world_up_pano"
    }
}

// MARK: - Tracker carry-over

public struct CarryoverTrackerState: Codable, Sendable {
    public let producedBySegment: String
    public let lastFrameIdx: Int
    public let activeTracks: [TrackEntry]
    public let nextTrackId: Int

    public struct TrackEntry: Codable, Sendable {
        public let trackId: Int
        public let kalmanState: KalmanStateEntry
        public let missingFrames: Int
        public let lastSeenFrameIdx: Int

        enum CodingKeys: String, CodingKey {
            case trackId = "track_id"
            case kalmanState = "kalman_state"
            case missingFrames = "missing_frames"
            case lastSeenFrameIdx = "last_seen_frame_idx"
        }
    }

    public struct KalmanStateEntry: Codable, Sendable {
        public let x: [Double]                // length 6
        public let PFlat: [[Double]]          // 6×6, JSON nested for readability

        enum CodingKeys: String, CodingKey {
            case x
            case PFlat = "P"
        }
    }

    enum CodingKeys: String, CodingKey {
        case producedBySegment = "produced_by_segment"
        case lastFrameIdx = "last_frame_idx"
        case activeTracks = "active_tracks"
        case nextTrackId = "next_track_id"
    }
}

// MARK: - Camera carry-over

public struct CarryoverCameraState: Codable, Sendable {
    public let smoothedYawDeg: Double?
    public let smoothedPitchDeg: Double?
    public let smoothedZoomFrac: Double?
    public let stationaryFrames: Int
    public let missingFrames: Int
    public let lastVelocityPxPerFrame: [Double]?  // [vx, vy] for EMA continuity

    enum CodingKeys: String, CodingKey {
        case smoothedYawDeg = "smoothed_yaw_deg"
        case smoothedPitchDeg = "smoothed_pitch_deg"
        case smoothedZoomFrac = "smoothed_zoom_frac"
        case stationaryFrames = "stationary_frames"
        case missingFrames = "missing_frames"
        case lastVelocityPxPerFrame = "last_velocity_px_per_frame"
    }
}

// MARK: - World-up pano carry-over (computed once per game)

public struct CarryoverWorldUpPano: Codable, Sendable {
    public let computedAtSegment: String
    public let mountTiltDeg: Double
    public let levelingRollDeg: Double
    public let fieldPolygon: [[Double]]   // [[x, y], ...]

    enum CodingKeys: String, CodingKey {
        case computedAtSegment = "computed_at_segment"
        case mountTiltDeg = "mount_tilt_deg"
        case levelingRollDeg = "leveling_roll_deg"
        case fieldPolygon = "field_polygon"
    }
}
