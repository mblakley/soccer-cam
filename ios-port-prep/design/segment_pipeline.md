# Segment pipeline — per-segment lifecycle on iOS

End-to-end lifecycle for one ~5-min segment: from "raw segment ready" to
"rendered output appended + raw deleted + carry-over written". This is the
core loop the iOS app runs once per segment as long as a Game is live.

## State machine

```
                  download done
                  ↓
   ┌─[ready_to_process]─┐
   │                    │
   │ Task spawned       ↓
   │              [detecting]
   │                    │
   │                    ↓
   │              [tracking]
   │                    │
   │                    ↓
   │              [rendering]   ←─── may fail; segment marked failed,
   │                    │            game continues with next segment
   │                    ↓
   │              [rendered]
   │                    │
   │ cleanup            ↓
   └─→[discarded]   (raw mp4 + intermediates deleted, rendered mp4 retained)
```

State transitions are written to `manifest.json` atomically (via
`JSONManifest.update { ... }`) so a crash at any point leaves a consistent
manifest the next launch can resume from.

## SegmentProcessor.swift (top-level orchestrator)

```swift
public actor SegmentProcessor {
    private let game: GameRef                       // weak-ish handle to parent Game actor
    private let detector: BallDetector
    private let tracker: BallTracker
    private let cameraStateMachine: CameraStateMachine
    private let renderer: MetalWarpRenderer
    private let encoder: VideoEncoder
    private let decoder: VideoDecoder

    /// Process one segment end-to-end. Returns the per-segment carry-over
    /// state to feed into the next segment. Throws on unrecoverable error;
    /// the caller marks the segment failed and continues.
    public func process(_ segment: Segment) async throws -> CarryoverState {
        try await markStatus(segment, .detecting)
        let detections = try await runDetect(segment)

        try await markStatus(segment, .tracking)
        let trajectory = try await runTrack(detections, segment: segment)

        try await markStatus(segment, .rendering)
        let renderedURL = try await runRender(
            segment: segment, trajectory: trajectory
        )

        try await markStatus(segment, .rendered)
        let carryover = makeCarryover(segment: segment)
        try await writeCarryover(carryover, segment: segment)

        try await cleanupRaw(segment)
        try await markStatus(segment, .discarded)

        return carryover
    }
}
```

## Stage 1 — Detect

```swift
private func runDetect(_ segment: Segment) async throws -> [Detection] {
    let sourceURL = segment.sourceURL!
    var detections: [Detection] = []
    var frameIdx = 0

    try await decoder.streamFrames(
        from: sourceURL,
        every: renderConfig.detectFrameInterval     // matches detect_frame_interval (default 4)
    ) { pixelBuffer, _ in
        let frameDetections = try await detector.detect(pixelBuffer)
        detections.append(contentsOf: frameDetections.map { d in
            Detection(
                x: d.x, y: d.y, confidence: d.confidence, frameIdx: frameIdx
            )
        })
        frameIdx += 1
        try Task.checkCancellation()
    }
    return detections
}
```

`BallDetector.detect` does the tiled inference (7×3 sliding window,
TILE_SIZE=640, STEP_X=576, STEP_Y=580 — match `ball_detector.py`).

## Stage 2 — Track

```swift
private func runTrack(
    _ detections: [Detection], segment: Segment
) async throws -> [TrajectoryEntry] {
    // tracker is per-Game (carries state across segments). Just feed it more frames.
    let byFrame = Dictionary(grouping: detections, by: \.frameIdx)
    let lastFrame = byFrame.keys.max() ?? 0
    for frameIdx in 0...lastFrame {
        let dets = byFrame[frameIdx] ?? []
        tracker.update(frameIdx: frameIdx, detections: dets)
        try Task.checkCancellation()
    }
    let best = tracker.bestTrack()
    var rawTrajectory: [[Double]?] = Array(repeating: nil, count: lastFrame + 1)
    if let best = best {
        for det in best.detections { rawTrajectory[det.frameIdx] = [det.x, det.y] }
        for p in best.predictions where rawTrajectory[p.frameIdx] == nil {
            rawTrajectory[p.frameIdx] = [p.x, p.y]
        }
    }
    return computeEntries(rawTrajectory: rawTrajectory, velocityEMA: mode.velocityEMA)
}
```

## Stage 3 — Render

```swift
private func runRender(segment: Segment, trajectory: [TrajectoryEntry])
    async throws -> URL
{
    let outURL = game.renderedURL(for: segment)
    let writer = try AVAssetWriter(outputURL: outURL, fileType: .mp4)
    let videoInput = AVAssetWriterInput(...)  // H.264 via VideoToolbox at renderConfig.bitrate
    let audioInput = AVAssetWriterInput(...)  // audio passthrough from source
    writer.add(videoInput)
    writer.add(audioInput)
    let pixelPool = makePixelBufferPool(width: outW, height: outH)

    writer.startWriting()
    writer.startSession(atSourceTime: .zero)

    var frameIdx = 0
    try await decoder.streamFrames(from: segment.sourceURL!, every: 1) { pixelBuffer, pts in
        try Task.checkCancellation()
        let entry = frameIdx < trajectory.count ? trajectory[frameIdx] : nil
        let (params, viewYaw) = cameraStateMachine.tick(entry: entry, frameIdx: frameIdx)
        let cropBox = computeCropBox(params: params, viewYawDeg: viewYaw)

        var destBuffer: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(nil, pixelPool, &destBuffer)
        try renderer.warp(
            source: pixelBuffer,
            destination: destBuffer!,
            cropBox: cropBox
        )

        // Hand to AVAssetWriter input for H.264 encode.
        try await videoInput.appendAsync(destBuffer!, presentationTime: pts)
        frameIdx += 1
    }
    // Audio: passthrough (no decode) for sample-accurate timing per E0.B7.
    try await audioInput.appendPassthroughAsync(from: segment.sourceURL!)

    videoInput.markAsFinished()
    audioInput.markAsFinished()
    await writer.finishWriting()
    if writer.status != .completed { throw RenderError.writerFailed(writer.error) }
    return outURL
}
```

## Stage 4 — Cleanup + carry-over

```swift
private func makeCarryover(segment: Segment) -> CarryoverState {
    CarryoverState(
        schemaVersion: 1,
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
            missingFrames: cameraStateMachine.state.missingFrames
        ),
        worldUpPano: game.worldUpPanoCarryover    // unchanged once computed
    )
}

private func cleanupRaw(_ segment: Segment) async throws {
    try FileManager.default.removeItem(at: segment.sourceURL!)
    // Per-segment intermediates (only written when debug logging is on)
    // are also wiped here.
}
```

## Final concatenation

When Game flips from `processing` → `complete`, concatenate all per-segment
`rendered_*.mp4` into one `final.mp4`. Use `AVAssetExportSession` with the
`AVAssetExportPresetPassthrough` preset — container-level concat, no re-
encode, near-instant.

```swift
public func finalize() async throws {
    let composition = AVMutableComposition()
    let videoTrack = composition.addMutableTrack(withMediaType: .video, ...)
    let audioTrack = composition.addMutableTrack(withMediaType: .audio, ...)
    var current = CMTime.zero
    for segment in segments.sortedBySequence {
        let asset = AVURLAsset(url: segment.renderedURL!)
        let duration = try await asset.load(.duration)
        try videoTrack.insertTimeRange(
            CMTimeRange(start: .zero, duration: duration),
            of: try await asset.loadTracks(withMediaType: .video).first!,
            at: current
        )
        try audioTrack.insertTimeRange(
            CMTimeRange(start: .zero, duration: duration),
            of: try await asset.loadTracks(withMediaType: .audio).first!,
            at: current
        )
        current = current + duration
    }
    let export = AVAssetExportSession(
        asset: composition, presetName: AVAssetExportPresetPassthrough
    )!
    export.outputURL = game.finalURL
    export.outputFileType = .mp4
    await export.export()
    if export.status != .completed { throw FinalizeError(export.error) }
    // After successful concat, delete the per-segment rendered files.
    for segment in segments {
        try FileManager.default.removeItem(at: segment.renderedURL!)
    }
}
```

## Error handling

- **Tracker fails on a segment** — keep best-effort partial trajectory,
  still try render with whatever entries we have. Log to manifest.
- **Render fails on a segment** — segment marked failed, carry-over NOT
  written (next segment starts cold for that aspect, won't crash). Game
  continues. Final mp4 will have a gap; UI shows missing segment block.
- **Cancellation mid-segment** — partial outputs deleted, segment reverts
  to `ready_to_process`, processing resumes next launch.
- **Cancellation mid-game** — current segment treated as cancellation;
  Game status flips to `cancelled` after segment cleanup completes.

## Cross-references

- `data_model.md#segment` — schema
- `swift_kalman_tracker.md` — `BallTracker.update`, `bestTrack`,
  `makeCarryover`
- `swift_camera_state_machine.md` — `CameraStateMachine.tick`
- `metal_warp_shader.md` — `MetalWarpRenderer.warp`
- `reolink_segment_ingest.md` — what produces `Segment` instances
- `ttt_api_integration.md` — destination for `final.mp4`
