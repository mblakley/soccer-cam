# TTT API integration — Swift client spec

The iOS app talks to TTT for: optional sign-in, model catalog + entitlement,
encrypted model download, and rendered-video upload. Per
[[project_ttt_login_optional]], the app is fully usable on bundled community
models with no login. Per [[feedback_ttt_hard_gate]], premium-model
entitlement is enforced server-side at the download endpoint.

Endpoints below match soccer-cam's existing TTTApiClient surface — the iOS
app is a different client against the same API. Per
[[reference_ttt_supabase_projects]] dev/preview work uses the preview
project; production uses prod.

## Files

```
Services/TTT/
├── TTTAPIClient.swift               # main URLSession-based client
├── TTTAPIError.swift                # typed error decoder
├── VideoUpload.swift                # rendered-mp4 upload (resumable)
└── TTTEnvironment.swift             # prod vs preview URLs
Services/Auth/
├── AuthService.swift                # ASWebAuthenticationSession + token store
└── TokenStore.swift                 # Keychain wrapper
Services/ModelCatalog/
└── TTTModelClient.swift             # model catalog + entitlement + blob download
```

## TTTEnvironment

```swift
public enum TTTEnvironment {
    case production
    case preview
    case local(URL)        // dev escape hatch per [[project_default_ttt_to_prod]]
                           // — local TTT only enabled via a debug-build setting

    var apiBaseURL: URL { ... }
    var supabaseURL: URL { ... }
    var supabaseAnonKey: String { ... }
}
```

The app defaults to `.production`. A debug-only Settings screen lets the
developer pick `.preview` or `.local(_)`. Per [[feedback_preview_before_prod]]
Mark's sideload builds default to `.preview`.

## Auth flow

Sign-in uses Supabase GoTrue's OAuth flow (the same one TTT web uses) via
`ASWebAuthenticationSession`. Per [[reference_supabase_gotrue_state]],
GoTrue forwards client `state` but doesn't persist it — we let GoTrue
generate state and defend CSRF via PKCE.

```swift
public actor AuthService {
    private let env: TTTEnvironment
    private let tokenStore: TokenStore

    public func signIn() async throws -> AuthSession {
        let pkce = PKCECodeChallenge.generate()
        let signInURL = env.supabaseURL
            .appendingPathComponent("/auth/v1/authorize")
            .appending(queryItems: [
                .init(name: "provider", value: "google"),
                .init(name: "redirect_to", value: callbackURLScheme + "://auth/callback"),
                .init(name: "code_challenge", value: pkce.challenge),
                .init(name: "code_challenge_method", value: "S256"),
            ])

        let callbackURL = try await ASWebAuthenticationSession
            .startWithPKCE(url: signInURL, scheme: callbackURLScheme)

        // Exchange the code for tokens.
        let code = callbackURL.queryItems["code"]!
        let tokens = try await exchangeCode(code, verifier: pkce.verifier)
        try tokenStore.store(access: tokens.access, refresh: tokens.refresh)
        return AuthSession(claims: try decodeJWT(tokens.access))
    }

    public func currentSession() async -> AuthSession? { ... }
    public func signOut() async { ... }

    /// Auto-refresh — called from a URLSession adapter before each protected request.
    func freshAccessToken() async throws -> String { ... }
}
```

`callbackURLScheme` is registered in the Info.plist (`CFBundleURLTypes`) as
`com.soccercam.ios`. The redirect URL configured on the TTT Supabase project
must include `com.soccercam.ios://auth/callback`.

## TTTAPIClient

```swift
public actor TTTAPIClient {
    private let env: TTTEnvironment
    private let auth: AuthService
    private let session: URLSession

    public func get<T: Decodable>(_ path: String, query: [URLQueryItem] = [],
                                  requiresAuth: Bool = true) async throws -> T { ... }
    public func post<T: Decodable, B: Encodable>(
        _ path: String, body: B, requiresAuth: Bool = true
    ) async throws -> T { ... }

    private func authorizedRequest(_ req: URLRequest) async throws -> URLRequest {
        var r = req
        let token = try await auth.freshAccessToken()
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return r
    }
}
```

## Model catalog + entitlement

```swift
public struct ModelCatalogEntry: Codable, Sendable {
    public let id: String
    public let displayName: String
    public let kind: ModelKind          // .free | .premium
    public let version: String
    public let bundleSizeBytes: Int64
    public let downloadEndpoint: String
}

public actor TTTModelClient {
    private let api: TTTAPIClient

    public func availableModels() async throws -> [ModelCatalogEntry] {
        try await api.get("/api/models", requiresAuth: false)
        // catalog is public; entitlement check happens at download time
    }

    public func modelEntitlement(modelId: String) async throws -> EntitlementContext {
        try await api.get("/api/models/\(modelId)/entitlement", requiresAuth: true)
    }

    /// Download the encrypted model blob. Server checks JWT entitlement
    /// at this endpoint and returns 403 if the user can't access this tier.
    public func downloadModelBlob(modelId: String, version: String)
        async throws -> EncryptedModelArtifact
    {
        let url = env.apiBaseURL
            .appendingPathComponent("/api/models/\(modelId)/blob")
            .appending(queryItems: [.init(name: "version", value: version)])
        let (data, response) = try await session.data(for: authorizedRequest(URLRequest(url: url)))
        let http = response as! HTTPURLResponse
        if http.statusCode == 403 {
            throw TTTAPIError.entitlementDenied
        }
        if http.statusCode != 200 {
            throw TTTAPIError.unexpectedStatus(http.statusCode)
        }
        return EncryptedModelArtifact(modelId: modelId, version: version, blob: data)
    }
}
```

The TTTAPIError surface maps server errors to typed cases the UI can react
to (entitlement denied → "Upgrade required" sheet; network error → retry).

## Video upload

Per [[feedback_use_existing_recording_handles]] the rendered mp4 is keyed
by `recording_group_dir` / `camera_recording_id` — not by `youtube_video_id`.
Per [[feedback_no_auto_youtube_cleanup]] we don't auto-upload to YouTube
either.

```swift
public actor VideoUploader {
    private let api: TTTAPIClient

    public struct UploadRequest: Encodable {
        public let gameId: String
        public let cameraRecordingId: String
        public let recordingGroupDir: String?     // optional metadata when known
        public let durationSeconds: Double
        public let renderedAtIso: String
        public let renderConfig: RenderedVideoConfig
    }

    /// Three-call upload flow matching the existing TTT API contract:
    /// 1) POST /api/games/{gameId}/videos/init  →  { uploadUrl, uploadId }
    /// 2) PUT to uploadUrl (chunked, resumable)
    /// 3) POST /api/games/{gameId}/videos/{uploadId}/finalize → { videoId }
    public func upload(
        _ url: URL, request: UploadRequest
    ) async throws -> String {
        let session = try await api.post(
            "/api/games/\(request.gameId)/videos/init", body: request
        )
        try await uploadChunked(localURL: url, to: session.uploadUrl,
                                resumeFromByte: session.resumeOffset ?? 0)
        let finalized = try await api.post(
            "/api/games/\(request.gameId)/videos/\(session.uploadId)/finalize",
            body: EmptyBody()
        )
        return finalized.videoId
    }
}
```

Chunked upload uses `URLSession.uploadTask(withStreamedRequest:)` with
~5 MB chunks and resume-from-byte support via the `Content-Range` header.
Background-eligible via `URLSessionConfiguration.background(withIdentifier:)`
so the upload survives the app being backgrounded.

## Network errors + retry

- 5xx — retry with exponential backoff (1s, 2s, 4s, 8s, give up after 4 tries)
- 401 — token expired; refresh via `AuthService` then retry once
- 403 — entitlement issue; bubble up to UI (don't retry)
- Connection lost / `URLError.notConnectedToInternet` — pause uploads,
  resume when reachability returns (`NWPathMonitor`)

## Telemetry — none in MVP

Per [[feedback_neutral_naming]] and the OSS posture, no analytics SDK in
the iOS app. Crash reports via Apple's built-in TestFlight / App Store
crash logs (no third-party crash reporter).

## Cross-references

- `cryptokit_decryption.md` — consumes `EncryptedModelArtifact` +
  `EntitlementContext`
- `data_model.md` — Keychain layout + JWT shape
- `app_ui.md#sign-in-flow` — the UI side of sign-in
- `segment_pipeline.md#finalize` — produces the mp4 the uploader sends
