// App/SoccerCamApp.swift
//
// SwiftUI app entry. Spins up the singleton GameManager and presents
// GamesListView at the root. See ios-port-prep/design/architecture.md for
// the dependency graph + threading model.

import SwiftUI

@main
struct SoccerCamApp: App {
    @StateObject private var gameManager = GameManagerStore()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(gameManager)
                .tint(.accentColor)
        }
    }
}

/// Bridge between the singleton GameManager actor and SwiftUI's
/// ObservableObject pattern. Holds a published snapshot of game state
/// observed via an AsyncStream from the actor.
@MainActor
final class GameManagerStore: ObservableObject {
    @Published private(set) var games: [GameSummary] = []
    private let manager: GameManager

    init() {
        // TODO: GameManager.init looks up the sandbox documents URL, loads
        // games_index.json, registers BGProcessingTask handlers.
        self.manager = GameManager()
        Task { await observeGameUpdates() }
    }

    private func observeGameUpdates() async {
        // TODO: for await snapshot in manager.snapshots { games = snapshot }
    }
}

public struct GameSummary: Identifiable, Sendable {
    public let id: String          // gameId
    public let displayName: String
    public let status: GameManifest.Status
    public let createdAt: Date
    public let updatedAt: Date
    public let thumbnailPath: String?
}
