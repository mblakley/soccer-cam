// Pipeline/SegmentProcessor.swift
//
// Per-segment orchestration: detect → track → render → encode → carry-over
// → cleanup. See ios-port-prep/design/segment_pipeline.md for the full
// state-machine spec.

import Foundation
import AVFoundation
import CoreVideo

public actor SegmentProcessor {
    private weak var game: GameActor?
    private let detector: BallDetector
    private let tracker: BallTracker
    private let cameraStateMachine: CameraStateMachine
    private let renderer: MetalWarpRenderer
    private let decoder: VideoDecoder

    public init(
        game: GameActor,
        detector: BallDetector,
        tracker: BallTracker,
        cameraStateMachine: CameraStateMachine,
        renderer: MetalWarpRenderer,
        decoder: VideoDecoder
    ) {
        self.game = game
        self.detector = detector
        self.tracker = tracker
        self.cameraStateMachine = cameraStateMachine
        self.renderer = renderer
        self.decoder = decoder
    }

    /// Drive one segment through the full pipeline. Returns the carry-over
    /// state to feed into segment N+1. Throws on unrecoverable error; the
    /// caller (Game actor) marks the segment failed and continues with the
    /// next one.
    public func process(_ segment: GameManifest.Segment) async throws -> CarryoverState {
        try Task.checkCancellation()

        try await mark(segment, status: .detecting)
        let detections = try await runDetect(segment)

        try Task.checkCancellation()

        try await mark(segment, status: .tracking)
        let trajectory = try await runTrack(detections, segment: segment)

        try Task.checkCancellation()

        try await mark(segment, status: .rendering)
        _ = try await runRender(segment: segment, trajectory: trajectory)

        try Task.checkCancellation()

        try await mark(segment, status: .rendered)
        let carryover = makeCarryover(for: segment)
        try await writeCarryover(carryover, segment: segment)

        try await cleanupRaw(segment)
        try await mark(segment, status: .discarded)

        return carryover
    }

    // MARK: - Stages

    private func runDetect(_ segment: GameManifest.Segment) async throws -> [Detection] {
        // TODO: decode every Nth frame (renderConfig.detectFrameInterval, default 4)
        // and feed each to detector.detect. Stream the results into a flat array
        // tagged with the absolute frame index.
        return []
    }

    private func runTrack(
        _ detections: [Detection],
        segment: GameManifest.Segment
    ) async throws -> [TrajectoryEntry] {
        // TODO: feed BallTracker.update per frame; pick bestTrack; pass through
        // computeEntries to get the per-frame (px, py, vx, vy) for the renderer.
        return []
    }

    private func runRender(
        segment: GameManifest.Segment,
        trajectory: [TrajectoryEntry]
    ) async throws -> URL {
        // TODO: decode every frame; for each:
        //   1) cameraStateMachine.tick(entry, frameIdx) → (params, viewYaw)
        //   2) cropBox(leveledPano, params, viewYaw)
        //   3) renderer.warp(source, destination, cropBox)
        //   4) AVAssetWriter input append with passthrough audio.
        // Returns URL of the per-segment rendered mp4.
        return URL(fileURLWithPath: "/dev/null")
    }

    // MARK: - Carry-over + cleanup

    private func makeCarryover(for segment: GameManifest.Segment) -> CarryoverState {
        // TODO: assemble from tracker.makeCarryover and cameraStateMachine.state
        // World-up pano is computed once per game; carryover carries it forward
        // unchanged after the first segment.
        return CarryoverState(
            producedBySegment: segment.id,
            producedAt: Date(),
            lastFrameIdx: cameraStateMachine.lastFrameIdx,
            trackerState: tracker.makeCarryover(
                producedBySegment: segment.id,
                lastFrameIdx: cameraStateMachine.lastFrameIdx
            ),
            cameraState: CarryoverCameraState(
                smoothedYawDeg: cameraStateMachine.state.smoothedYawDeg,
                smoothedPitchDeg: cameraStateMachine.state.smoothedPitchDeg,
                smoothedZoomFrac: cameraStateMachine.state.smoothedZoomFrac,
                stationaryFrames: cameraStateMachine.state.stationaryFrames,
                missingFrames: cameraStateMachine.state.missingFrames,
                lastVelocityPxPerFrame: nil
            ),
            worldUpPano: nil
        )
    }

    private func writeCarryover(_ s: CarryoverState, segment: GameManifest.Segment) async throws {
        // TODO
    }

    private func cleanupRaw(_ segment: GameManifest.Segment) async throws {
        // TODO: delete the raw segment mp4 and any per-segment intermediates.
        // Keep the rendered mp4 — that's the output we're producing.
    }

    private func mark(_ segment: GameManifest.Segment, status: GameManifest.Segment.Status) async throws {
        try await game?.updateSegmentStatus(id: segment.id, status: status)
    }
}

// MARK: - Game actor + service-stub placeholders

public protocol GameActor: AnyObject, Sendable {
    func updateSegmentStatus(id: String, status: GameManifest.Segment.Status) async throws
}

public actor GameManager {
    public init() {}
    // TODO: see architecture.md — owns the active games, persists games_index.json,
    // registers BGProcessingTask handlers, broadcasts snapshots to UI.
}

public actor BallDetector {
    public init(modelURL: URL) {}
    public func detect(_ pixelBuffer: CVPixelBuffer) async throws -> [Detection] {
        // TODO: tiled inference via VNCoreMLRequest — replicate ball_detector.py
        // tiling exactly (7×3, TILE_SIZE=640, STEP_X=576, STEP_Y=580).
        return []
    }
}

public actor VideoDecoder {
    public init() {}
    public func streamFrames(
        from url: URL,
        every interval: Int,
        body: (CVPixelBuffer, CMTime) async throws -> Void
    ) async throws {
        // TODO: AVAssetReader + AVAssetReaderTrackOutput with kCVPixelFormatType_32BGRA.
    }
}

public actor MetalWarpRenderer {
    public init(leveledPano: LeveledPano) throws {
        // TODO: see metal_warp_shader.md — upload Lx/Ly to MTLBuffer once,
        // compile the warp pipeline state.
    }
    public func warp(
        source: CVPixelBuffer,
        destination: CVPixelBuffer,
        cropBox: (x: Int, y: Int, w: Int, h: Int)
    ) async throws {
        // TODO: dispatch WarpKernel.metal — see metal_warp_shader.md.
    }
}
