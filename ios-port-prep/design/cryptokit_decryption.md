# CryptoKit decryption — Swift port spec

Swift `CryptoKit` equivalent of the AES-GCM decryption logic in
`video_grouper/ball_tracking/secure_loader.py`. Per
[[feedback_no_security_docs_in_oss]], this doc covers the **mechanics only**
— the threat model, key-rotation strategy, and forensic-watermarking
rationale live in the TTT private repo, not here.

**Invariant:** decrypted model bytes never touch disk. The decrypted `Data`
flows straight into `MLModel(modelData:)` (or, on iOS SDK versions where
that constructor is missing, a temp file in a `URLProtectionType.complete`
container that's wiped immediately after `MLModel` load).

## File

```
Services/Decryption/CryptoKitLoader.swift
```

## What it ports

The Python `SecureLoader.acquire(model_key, channel, pipeline_version)`
does:

1. Request a license + entitlement from TTT for `model_key`.
2. Download the encrypted artifact (the `.enc` blob).
3. Derive the decryption key from JWT claims + a per-version salt.
4. Decrypt in memory using AES-GCM.
5. Pass plaintext to `ort.InferenceSession(plaintext, providers=...)`.

The Swift port mirrors steps 3–5; steps 1–2 are HTTP calls covered by
`ttt_api_integration.md`.

## Encrypted artifact format

(Documented in soccer-cam's `secure_loader.py` already — repeating only the
on-the-wire layout, no rationale.)

```
[ 4 bytes magic = b"SCM1" ]
[ 4 bytes version (uint32 BE) ]
[ 12 bytes nonce ]
[ 16 bytes auth tag ]
[ N bytes ciphertext = AES-GCM(plaintext_model_bytes) ]
```

The encryption key is derived from `JWT.sub + JWT.entitlement_id + version_salt`
via HKDF-SHA256 (32-byte output) — exact derivation matches secure_loader.py
`_derive_key`. Inputs are read from the JWT decoded earlier by the auth flow.

## API

```swift
import Foundation
import CryptoKit
import CoreML

public struct EncryptedModelArtifact: Sendable {
    public let modelId: String
    public let version: String
    public let blob: Data                    // the full SCM1 blob from the wire
}

public struct EntitlementContext: Sendable {
    public let subject: String               // JWT sub
    public let entitlementId: String
    public let versionSalt: Data             // bound to the model version
}

public enum DecryptError: Error {
    case badMagic
    case unsupportedVersion(UInt32)
    case truncated
    case authentication                      // GCM tag mismatch — tampered or wrong key
    case mlModelLoad(Error)
}

public enum CryptoKitLoader {
    /// Decrypt the artifact in-memory and load a CoreML model from it.
    /// The plaintext exists only on the stack-allocated Data; cleared
    /// before return when possible.
    public static func loadModel(
        artifact: EncryptedModelArtifact,
        entitlement: EntitlementContext
    ) throws -> MLModel {
        let parsed = try parseSCM1(artifact.blob)
        let key = try deriveKey(
            subject: entitlement.subject,
            entitlementId: entitlement.entitlementId,
            versionSalt: entitlement.versionSalt
        )
        let nonce = try AES.GCM.Nonce(data: parsed.nonce)
        let sealed = try AES.GCM.SealedBox(
            nonce: nonce,
            ciphertext: parsed.ciphertext,
            tag: parsed.tag
        )
        let plaintext: Data
        do {
            plaintext = try AES.GCM.open(sealed, using: key)
        } catch {
            throw DecryptError.authentication
        }

        // Load CoreML model directly from in-memory Data.
        // iOS 17+ supports MLModel(asset:) and MLModelAsset(memory:configuration:)
        // — use it when available so we never write plaintext to disk.
        do {
            let modelAsset = try MLModelAsset(memory: plaintext, configuration: nil)
            return try MLModel(asset: modelAsset, configuration: defaultConfig())
        } catch {
            throw DecryptError.mlModelLoad(error)
        }
    }
}

private struct ParsedSCM1 {
    let version: UInt32
    let nonce: Data    // 12 bytes
    let tag: Data      // 16 bytes
    let ciphertext: Data
}

private func parseSCM1(_ blob: Data) throws -> ParsedSCM1 {
    guard blob.count >= 4 + 4 + 12 + 16 else { throw DecryptError.truncated }
    let magic = blob[0..<4]
    guard magic == Data("SCM1".utf8) else { throw DecryptError.badMagic }
    let version = blob[4..<8].withUnsafeBytes {
        UInt32(bigEndian: $0.load(as: UInt32.self))
    }
    guard version == 1 else { throw DecryptError.unsupportedVersion(version) }
    let nonce = blob[8..<20]
    let tag = blob[20..<36]
    let ciphertext = blob[36...]
    return ParsedSCM1(version: version, nonce: Data(nonce),
                      tag: Data(tag), ciphertext: Data(ciphertext))
}

private func deriveKey(
    subject: String, entitlementId: String, versionSalt: Data
) throws -> SymmetricKey {
    // HKDF-SHA256, 32-byte output. Matches Python _derive_key.
    let inputKeyingMaterial = Data((subject + "|" + entitlementId).utf8)
    let derived = HKDF<SHA256>.deriveKey(
        inputKeyMaterial: SymmetricKey(data: inputKeyingMaterial),
        salt: versionSalt,
        info: Data("soccer-cam.model.v1".utf8),
        outputByteCount: 32
    )
    return derived
}

private func defaultConfig() -> MLModelConfiguration {
    let c = MLModelConfiguration()
    c.computeUnits = .all   // ANE + GPU + CPU per E0.A6 measurements
    return c
}
```

## Fallback path — `MLModelAsset(memory:)` not available

iOS 16 doesn't expose the in-memory `MLModelAsset` constructor. Fallback:

```swift
private static func loadViaTempFile(plaintext: Data) throws -> MLModel {
    // A protected per-launch temp directory, scrubbed eagerly.
    let dir = try FileManager.default.url(
        for: .itemReplacementDirectory, in: .userDomainMask,
        appropriateFor: try FileManager.default.url(
            for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil, create: true
        ),
        create: true
    )
    let modelURL = dir.appendingPathComponent("model.mlpackage")
    try plaintext.write(to: modelURL, options: [.atomic, .completeFileProtection])
    defer {
        // Overwrite then remove. iOS encrypts at-rest, but explicit erase + remove
        // bounds the window even in a forensic scenario.
        try? Data(repeating: 0, count: plaintext.count).write(to: modelURL)
        try? FileManager.default.removeItem(at: dir)
    }
    let model = try MLModel(contentsOf: modelURL, configuration: defaultConfig())
    return model
}
```

**Minimum-supported-iOS decision:** target iOS 17.0 so the in-memory path is
always available, avoiding the fallback. iOS 16 share is small enough by
the time the app ships that this is a non-issue.

## Token / entitlement source

The `EntitlementContext` comes from the TTT auth flow's parsed JWT — see
`ttt_api_integration.md`. The Loader doesn't talk to TTT directly; the
caller (`ModelCatalog`) hands it the artifact + entitlement.

```swift
// At ModelCatalog level:
let entitlement = try await tttClient.modelEntitlement(modelId: m.id)
let artifact = try await tttClient.downloadModelBlob(modelId: m.id, version: m.version)
let model = try CryptoKitLoader.loadModel(
    artifact: artifact, entitlement: entitlement
)
```

## Cache policy

Encrypted artifacts cached at `Library/Caches/models/<modelId>_<version>.enc`,
excluded from iCloud backup via `URLResourceKey.isExcludedFromBackupKey`.
On signed-out / signed-in-different-account state changes, the entire cache
is wiped (next download required). Cache eviction follows iOS's normal
Caches-directory behavior (LRU under disk pressure).

## Cross-references

- `ttt_api_integration.md` — provides `EncryptedModelArtifact` +
  `EntitlementContext`
- `data_model.md` — Keychain layout for the JWT
- `architecture.md#dependencies` — `CryptoKit` is the only crypto dep
