// Services/Decryption/CryptoKitLoader.swift
//
// AES-GCM model decryption, Swift CryptoKit equivalent of
// video_grouper/ball_tracking/secure_loader.py (mechanics only — threat
// model lives in TTT private docs per [[feedback_no_security_docs_in_oss]]).
//
// Invariant: decrypted plaintext never touches disk. See
// ios-port-prep/design/cryptokit_decryption.md.

import Foundation
import CryptoKit
import CoreML

public struct EncryptedModelArtifact: Sendable {
    public let modelId: String
    public let version: String
    public let blob: Data
}

public struct EntitlementContext: Sendable {
    public let subject: String
    public let entitlementId: String
    public let versionSalt: Data
}

public enum DecryptError: Error {
    case badMagic
    case unsupportedVersion(UInt32)
    case truncated
    case authentication
    case mlModelLoad(Error)
}

public enum CryptoKitLoader {
    /// Decrypt the artifact in-memory and load a CoreML model from it.
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
            nonce: nonce, ciphertext: parsed.ciphertext, tag: parsed.tag
        )
        let plaintext: Data
        do {
            plaintext = try AES.GCM.open(sealed, using: key)
        } catch {
            throw DecryptError.authentication
        }

        // TODO: load via in-memory MLModelAsset(memory:configuration:) on
        // iOS 17+. Fallback to temp-file path on iOS 16 — see design doc.
        _ = plaintext
        throw DecryptError.mlModelLoad(NSError(
            domain: "soccer-cam-ios", code: -1,
            userInfo: [NSLocalizedDescriptionKey: "TODO: wire MLModel load"]
        ))
    }
}

private struct ParsedSCM1 {
    let version: UInt32
    let nonce: Data
    let tag: Data
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
    return ParsedSCM1(
        version: version,
        nonce: Data(nonce),
        tag: Data(tag),
        ciphertext: Data(ciphertext)
    )
}

private func deriveKey(
    subject: String, entitlementId: String, versionSalt: Data
) throws -> SymmetricKey {
    // HKDF-SHA256 → 32-byte key. Mirrors secure_loader.py _derive_key.
    let inputKeyingMaterial = Data((subject + "|" + entitlementId).utf8)
    return HKDF<SHA256>.deriveKey(
        inputKeyMaterial: SymmetricKey(data: inputKeyingMaterial),
        salt: versionSalt,
        info: Data("soccer-cam.model.v1".utf8),
        outputByteCount: 32
    )
}
