# Reolink segment ingest — Swift downloader spec

Pull ~5-min mp4 segments from a Reolink camera on the field's Wi-Fi as they
become available, queue them for the per-segment pipeline. Mirror of
`video_grouper/task_processors/download_processor.py` and
`video_grouper/cameras/reolink.py` from soccer-cam — same camera API, same
segment-discovery semantics, Swift host.

## Files

```
Services/ReolinkIngest/
├── SegmentDownloader.swift       # actor — polls + downloads
├── ReolinkAPI.swift              # /api/cgi requests + responses
└── BulkImportSource.swift        # the "user picked a file" alternative source
```

## Reolink API surface

Soccer-cam's reolink.py talks to the camera's CGI endpoint over HTTP. The
two calls we need:

1. **`Search`** — list recordings in a time range. Returns segment metadata
   (filename, start time, size, channel).
2. **`Snap` / `Playback`** — download a specific segment by filename.

Reuse exact request shape from soccer-cam's reference. Auth is
username+password (passed in URL or HTTP digest depending on firmware).

```swift
public struct ReolinkConfig: Codable, Sendable {
    public let baseURL: URL              // e.g. https://192.168.1.42 (the field AP)
    public let username: String
    public let channel: Int              // typically 0
}

public actor ReolinkAPIClient {
    private let config: ReolinkConfig
    private let session: URLSession
    private let passwordKey: String      // keychain account key

    public init(config: ReolinkConfig) {
        self.config = config
        // Allow self-signed certs from the camera AP — pinning is not viable
        // for a personal device on a private network. Document for App Store
        // review: this trust applies ONLY to URLSession instances scoped to
        // ReolinkAPIClient, never to the TTT/general-internet client.
        var c = URLSessionConfiguration.default
        c.tlsMinimumSupportedProtocol = .tlsProtocol12
        self.session = URLSession(configuration: c, delegate: ReolinkTrustDelegate(), delegateQueue: nil)
        self.passwordKey = "reolink/\(config.baseURL.host ?? "unknown")"
    }

    public func listSegments(since: Date) async throws -> [ReolinkSegmentMeta] { ... }
    public func download(segment: ReolinkSegmentMeta, to url: URL,
                         progress: ((Double) -> Void)?) async throws { ... }
}

public struct ReolinkSegmentMeta: Hashable, Codable, Sendable {
    public let filename: String          // camera-side filename (the unique key)
    public let startTime: Date           // segment's recorded wall-clock start
    public let durationSeconds: Double
    public let sizeBytes: Int64
}
```

## SegmentDownloader

One per Game. Polls the camera every `pollInterval` (default 30 s), emits
new segments downstream as they appear and finish downloading. Resilient
to network drops — retries with exponential backoff.

```swift
public actor SegmentDownloader {
    private let api: ReolinkAPIClient
    private let game: GameRef
    private let pollInterval: TimeInterval
    private var seenSegments: Set<String> = []
    private var pollTask: Task<Void, Never>?

    public init(api: ReolinkAPIClient, game: GameRef, pollInterval: TimeInterval = 30) {
        self.api = api
        self.game = game
        self.pollInterval = pollInterval
    }

    public func start() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            await self?.runLoop()
        }
    }

    public func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    private func runLoop() async {
        var since = Date().addingTimeInterval(-300)   // catch the most recent ~5 min on first poll
        while !Task.isCancelled {
            do {
                let segments = try await api.listSegments(since: since)
                let new = segments.filter { !seenSegments.contains($0.filename) }
                for seg in new.sorted(by: { $0.startTime < $1.startTime }) {
                    try await downloadAndEnqueue(seg)
                    seenSegments.insert(seg.filename)
                    since = seg.startTime
                }
            } catch {
                game.log(.warn, "reolink poll failed: \(error)")
                try? await Task.sleep(for: .seconds(backoffDelay()))
                continue
            }
            try? await Task.sleep(for: .seconds(pollInterval))
        }
    }

    private func downloadAndEnqueue(_ meta: ReolinkSegmentMeta) async throws {
        let segment = await game.createSegment(meta: meta)   // assigns sequence, paths
        try await game.markSegmentStatus(segment, .downloading)
        let dest = await game.segmentSourceURL(segment)
        try await api.download(segment: meta, to: dest) { _ in
            // progress updates flow to UI via the manifest — coarse enough that
            // we just update bytesDownloaded every 5%
        }
        try await game.markSegmentStatus(segment, .readyToProcess)
    }
}
```

## End-of-game detection

Game flips to `processing` (i.e. stop polling, just finish what's queued)
when:

- User explicitly taps "Mark game complete" (Settings → current game)
- OR 2 consecutive polls return no new segments AND the user hasn't
  interacted for >5 min — soft heuristic; user can always re-open the game

When `processing` completes (all queued segments rendered), Game runs
`finalize()` (see `segment_pipeline.md`).

## Bulk-import source

The fallback when the user has an already-recorded panorama mp4 (Veo
download, post-game AirDrop, etc.). Same pipeline, virtual chunker.

```swift
public actor BulkImportSource {
    private let game: GameRef
    private let sourceURL: URL                    // the imported full-game mp4
    private let virtualSegmentSeconds: Double = 300   // chunk into 5-min virtual segments

    public func enqueueAll() async throws {
        let asset = AVURLAsset(url: sourceURL)
        let duration = try await asset.load(.duration).seconds
        var start = 0.0
        var seq = 0
        while start < duration {
            let length = min(virtualSegmentSeconds, duration - start)
            // Use AVAssetExportSession to slice; lossless passthrough.
            let segMeta = try await sliceSegment(start: start, length: length, sequence: seq)
            let segment = await game.createSegment(meta: segMeta)
            try await game.markSegmentStatus(segment, .readyToProcess)
            seq += 1
            start += length
        }
    }
}
```

After all virtual segments are produced and processed, Game finalizes —
same code path as the Reolink flow.

## Wi-Fi reliability notes

E0.D2 measures sustained Wi-Fi throughput from camera to iPhone. Two
deployment modes:

- **Camera AP** — iPhone connects to the camera's own Wi-Fi network. Best
  for range; worst for parallel internet (camera AP doesn't bridge). User
  has to remember to switch back for TTT auth/upload. Document in setup.
- **Shared field SSID** — both camera and iPhone on the same upstream
  Wi-Fi. Best of both worlds when available. Many fields don't have it.
- **Phone hotspot** — iPhone hotspots → camera connects to iPhone. Drains
  iPhone battery faster, but unlocks LTE upload + processing concurrently.

The `ReolinkConfig` doesn't know or care which mode. The app's onboarding
helps the user pick at setup time.

## Cross-references

- `data_model.md#game_manifest.json#source` — config shape
- `segment_pipeline.md` — what happens after `readyToProcess`
- `app_ui.md#sign-in-and-setup` — the setup flow that captures Reolink config
