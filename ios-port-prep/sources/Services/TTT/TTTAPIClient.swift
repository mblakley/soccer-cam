// Services/TTT/TTTAPIClient.swift
//
// Thin URLSession-based client against the TTT REST API. See
// ios-port-prep/design/ttt_api_integration.md for the endpoints. Per
// [[project_default_ttt_to_prod]], defaults to TTT production; debug
// builds expose preview/local via TTTEnvironment.

import Foundation

public enum TTTEnvironment: Sendable {
    case production
    case preview
    case local(URL)

    public var apiBaseURL: URL {
        switch self {
        case .production:
            return URL(string: "https://api.teamtechtools.com")!
        case .preview:
            return URL(string: "https://api-preview.teamtechtools.com")!  // TODO: verify
        case .local(let url):
            return url
        }
    }

    public var supabaseURL: URL {
        switch self {
        case .production:
            return URL(string: "https://zmuwmngqqiaectpcqlfj.supabase.co")!
        case .preview:
            return URL(string: "https://opirofrhbffpszrnzsyp.supabase.co")!
        case .local:
            return URL(string: "http://127.0.0.1:54321")!
        }
    }

    /// Anon (publishable) Supabase key. Production value lives in CI/Xcode
    /// build settings, NOT checked in. Preview/local values can be checked
    /// in — they're public publishable keys.
    public var supabaseAnonKey: String {
        // TODO: read from Info.plist (build-injected per
        // [[feedback_build_time_config]]).
        return ""
    }
}

public enum TTTAPIError: Error, Sendable {
    case unauthorized                  // 401 — token expired or invalid
    case entitlementDenied             // 403 — premium gate
    case unexpectedStatus(Int)
    case decodeFailed(Error)
    case network(URLError)
}

public actor TTTAPIClient {
    private let env: TTTEnvironment
    private let auth: AuthService
    private let session: URLSession

    public init(env: TTTEnvironment = .production, auth: AuthService) {
        self.env = env
        self.auth = auth
        var config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        self.session = URLSession(configuration: config)
    }

    public func get<T: Decodable>(
        _ path: String,
        query: [URLQueryItem] = [],
        requiresAuth: Bool = true
    ) async throws -> T {
        let url = env.apiBaseURL
            .appendingPathComponent(path)
            .appending(queryItems: query)
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        if requiresAuth {
            req = try await authorized(req)
        }
        return try await perform(req)
    }

    public func post<T: Decodable, B: Encodable>(
        _ path: String,
        body: B,
        requiresAuth: Bool = true
    ) async throws -> T {
        let url = env.apiBaseURL.appendingPathComponent(path)
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        if requiresAuth {
            req = try await authorized(req)
        }
        return try await perform(req)
    }

    private func authorized(_ req: URLRequest) async throws -> URLRequest {
        var r = req
        let token = try await auth.freshAccessToken()
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return r
    }

    private func perform<T: Decodable>(_ req: URLRequest) async throws -> T {
        do {
            let (data, response) = try await session.data(for: req)
            let http = response as! HTTPURLResponse
            switch http.statusCode {
            case 200..<300:
                do {
                    return try JSONDecoder.iso8601().decode(T.self, from: data)
                } catch {
                    throw TTTAPIError.decodeFailed(error)
                }
            case 401: throw TTTAPIError.unauthorized
            case 403: throw TTTAPIError.entitlementDenied
            default: throw TTTAPIError.unexpectedStatus(http.statusCode)
            }
        } catch let urlError as URLError {
            throw TTTAPIError.network(urlError)
        }
    }
}

// MARK: - Auth (stub — see ios-port-prep/design/ttt_api_integration.md)

public actor AuthService {
    public init() {}
    public func freshAccessToken() async throws -> String {
        // TODO: read JWT from Keychain; refresh if within 5 min of expiry.
        return ""
    }
    public func signIn() async throws -> AuthSession {
        // TODO: ASWebAuthenticationSession + PKCE — see design doc.
        throw NSError(domain: "soccer-cam-ios.auth", code: -1)
    }
    public func signOut() async {
        // TODO
    }
}

public struct AuthSession: Sendable {
    public let claims: JWTClaims
}

public struct JWTClaims: Codable, Sendable {
    public let sub: String
    public let exp: TimeInterval
    public let email: String?
}

// MARK: - URL helper

extension URL {
    func appending(queryItems items: [URLQueryItem]) -> URL {
        guard !items.isEmpty else { return self }
        var components = URLComponents(url: self, resolvingAgainstBaseURL: false)!
        components.queryItems = (components.queryItems ?? []) + items
        return components.url!
    }
}
