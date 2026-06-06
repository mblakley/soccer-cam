// Domain/Manifest.swift
//
// Per-game manifest persisted at Documents/games/<gameId>/manifest.json.
// Schema parity with ios-port-prep/design/data_model.md#game_manifest.json.

import Foundation

public struct GameManifest: Codable, Sendable {
    public let schemaVersion: Int
    public let gameId: String                     // ULID
    public var tttGameId: String?                 // set after TTT upload
    public var displayName: String
    public let createdAt: Date
    public var completedAt: Date?
    public var status: Status

    public var source: Source
    public var settings: Settings
    public var segments: [Segment]
    public var finalOutput: FinalOutput
    public var error: ErrorPayload?

    public enum Status: String, Codable, Sendable {
        case pending, downloading, processing, complete, uploaded, cancelled, failed
    }

    public struct Source: Codable, Sendable {
        public var kind: Kind
        public var reolink: Reolink?
        public var bulkImport: BulkImport?

        public enum Kind: String, Codable, Sendable {
            case reolink, bulkImport = "bulk_import"
        }

        public struct Reolink: Codable, Sendable {
            public var baseURL: URL
            public var username: String
            public var channel: Int
            enum CodingKeys: String, CodingKey {
                case baseURL = "base_url"
                case username
                case channel
            }
        }

        public struct BulkImport: Codable, Sendable {
            public var originalFilename: String
            public var importedAt: Date
            enum CodingKeys: String, CodingKey {
                case originalFilename = "original_filename"
                case importedAt = "imported_at"
            }
        }

        enum CodingKeys: String, CodingKey {
            case kind, reolink, bulkImport = "bulk_import"
        }
    }

    public struct Settings: Codable, Sendable {
        public var modelSource: ModelSource
        public var renderMode: RenderMode
        public var outputResolution: [Int]  // [w, h]

        public enum RenderMode: String, Codable, Sendable {
            case broadcast, coach
        }

        public struct ModelSource: Codable, Sendable {
            public var kind: Kind
            public var modelId: String

            public enum Kind: String, Codable, Sendable {
                case bundled, tttFree = "ttt_free", tttPremium = "ttt_premium"
            }

            enum CodingKeys: String, CodingKey {
                case kind, modelId = "model_id"
            }
        }

        enum CodingKeys: String, CodingKey {
            case modelSource = "model_source"
            case renderMode = "render_mode"
            case outputResolution = "output_resolution"
        }
    }

    public struct Segment: Codable, Sendable, Identifiable {
        public var id: String                  // "segment_001"
        public var sequence: Int
        public var status: Status

        public var sourcePath: String?         // relative; nil after deletion
        public var renderedPath: String?
        public var carryoverPath: String?

        public var sourceBytes: Int64?
        public var sourceDurationSeconds: Double?
        public var sourceStartedAt: Date?

        public var frameCount: Int?
        public var detectionCount: Int?

        public var timingsMs: TimingsMs?
        public var startedAt: Date?
        public var completedAt: Date?

        public enum Status: String, Codable, Sendable {
            case pendingDownload = "pending_download"
            case downloading
            case readyToProcess = "ready_to_process"
            case detecting, tracking, rendering
            case rendered, discarded, failed
        }

        public struct TimingsMs: Codable, Sendable {
            public var downloadedMs: Int?
            public var detectMs: Int?
            public var trackMs: Int?
            public var renderMs: Int?

            enum CodingKeys: String, CodingKey {
                case downloadedMs = "downloaded_ms"
                case detectMs = "detect_ms"
                case trackMs = "track_ms"
                case renderMs = "render_ms"
            }
        }

        enum CodingKeys: String, CodingKey {
            case id = "segment_id"
            case sequence, status
            case sourcePath = "source_path"
            case renderedPath = "rendered_path"
            case carryoverPath = "carryover_path"
            case sourceBytes = "source_bytes"
            case sourceDurationSeconds = "source_duration_seconds"
            case sourceStartedAt = "source_started_at"
            case frameCount = "frame_count"
            case detectionCount = "detection_count"
            case timingsMs = "timings_ms"
            case startedAt = "started_at"
            case completedAt = "completed_at"
        }
    }

    public struct FinalOutput: Codable, Sendable {
        public var path: String?
        public var durationSeconds: Double?
        public var uploadedToTtt: Bool
        public var uploadedVideoId: String?

        enum CodingKeys: String, CodingKey {
            case path
            case durationSeconds = "duration_seconds"
            case uploadedToTtt = "uploaded_to_ttt"
            case uploadedVideoId = "uploaded_video_id"
        }
    }

    public struct ErrorPayload: Codable, Sendable {
        public var code: String
        public var message: String
        public var occurredAt: Date
        public var failedSegmentId: String?

        enum CodingKeys: String, CodingKey {
            case code, message
            case occurredAt = "occurred_at"
            case failedSegmentId = "failed_segment_id"
        }
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case gameId = "game_id"
        case tttGameId = "ttt_game_id"
        case displayName = "display_name"
        case createdAt = "created_at"
        case completedAt = "completed_at"
        case status
        case source
        case settings
        case segments
        case finalOutput = "final_output"
        case error
    }
}

// MARK: - Atomic load/save

public extension GameManifest {
    /// Load manifest from disk, applying any pending schema migrations.
    static func load(from url: URL) throws -> GameManifest {
        // TODO: read; peek schema_version; run migrations sequentially.
        let data = try Data(contentsOf: url)
        return try JSONDecoder.iso8601().decode(GameManifest.self, from: data)
    }

    /// Atomic write — temp file + rename, fsync the directory.
    func save(to url: URL) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .prettyPrinted]
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(self)
        // TODO: write to temp, rename atomic; per AtomicJSON pattern in
        // soccer-cam's utils/atomic_json.py.
        try data.write(to: url, options: .atomic)
    }
}

extension JSONDecoder {
    static func iso8601() -> JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }
}
