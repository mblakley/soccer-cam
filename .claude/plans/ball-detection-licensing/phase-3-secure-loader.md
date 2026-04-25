# Phase 3 — Secure Loader (soccer-cam side) [DONE 2026-04-25]

Branch: `feature/ball-detection-loader` (worktree off `origin/main`)
Started: 2026-04-25

Phase 3 of the cross-repo ball-detection-licensing rollout. The full plan
lives outside this repo at
`~/.claude/plans/explain-how-soccer-cam-s-design-robust-avalanche.md`.
This file scopes the soccer-cam-side deliverable only; threat model and
security rationale stay in TTT (per the OSS split).

## Goal

Make soccer-cam able to acquire a ball-detection model license from TTT,
download the encrypted artifact, decrypt it, and produce an
`onnxruntime.InferenceSession` ready for inference. No bundled model;
TTT account is required for any ball detection (free or premium).

## What landed

### `video_grouper/ball_tracking/secure_loader.py`

Pure Python loader (replaced by a Cython native module in Phase 5).
Single entrypoint: `SecureLoader(ttt_client, public_keys).acquire(model_key, channel=..., pipeline_version=...) -> LoadedModel`.

Steps:
1. `ttt_client.acquire_model_license(...)` → license response with manifest + signature + wrapped_key
2. Verify Ed25519 signature on the canonical-JSON manifest against any key in the configured public-key list (rotation-friendly)
3. Verify `user_id`, `expires_at` claims
4. GET the `artifact_url` from the manifest
5. Verify `artifact_sha256` matches downloaded bytes
6. Parse the binary container (magic + format + header + ciphertext)
7. Verify header `model_key` and `version` match the license
8. AES-GCM decrypt with the wrapped_key + AAD `(model_key, version, master_key_id)` → plaintext ONNX bytes
9. Build `onnxruntime.InferenceSession` with provider fallback `CUDA → DirectML → CPU`
10. Return `LoadedModel(session, model_key, version, tier, provider)`

The `tier` field on the returned `LoadedModel` lets callers (e.g. tray UI)
detect a free-tier downshift after a Stripe lapse without needing a
separate API call.

`onnxruntime` import is lazy — the module is importable even if the
runtime DLL fails to load (common on dev machines without DirectX
runtime). Tests mock the lazy `_onnxruntime()` accessor.

### `video_grouper/api_integrations/ttt_api.py`

Two new methods on `TTTApiClient`:

- `list_model_versions(model_key, channel=None, pipeline_version=None)` →
  GET `/api/models/{key}/versions` with optional query params
- `acquire_model_license(model_key, channel=None, pipeline_version=None)` →
  POST `/api/models/{key}/license` with optional body params

Both reuse the existing `_request` helper and inherit the same auth /
auto-refresh behavior as every other method on the client. Returns 403
for unentitled / unauthenticated users — the loader surfaces this as
`SecureLoaderError`.

### `tests/test_ball_tracking_secure_loader.py`

19 unit tests:

- **Happy path** — entitled user gets a session; `LoadedModel` has the right `tier`/`version`; `channel` / `pipeline_version` pass through
- **License verification** — wrong signing key, expired license, mismatched user_id, multi-key public-key list (rotation)
- **Artifact tampering** — version flipped in header, SHA mismatch, bad magic, wrong wrapped_key (all → `SecureLoaderError`)
- **Auth + transport** — unauthenticated client, TTT errors propagate, HTTP 500 on artifact download
- **Provider selection** — CUDA preferred, falls back to DirectML, falls back to CPU, empty list defaults to CPU
- **Tier observability** — premium and free tiers surface in `LoadedModel.tier`

## Out of scope for Phase 3

- **`homegrown` ball-tracking provider** — the actual AutoCam-replacement
  provider that consumes a `LoadedModel` and produces broadcast video.
  That's a separate ML problem (the YOLO detector + camera-following
  logic), not a licensing/security concern.
- **Tray UI tier-downshift notification** — the loader exposes `tier` on
  `LoadedModel`; the tray app wiring is a separate piece.
- **Cython native module** — Phase 5.
- **Background license refresh** — current loader acquires on-demand.
  A long-running app would want to refresh proactively before expiry.

## Verification

- `uv run ruff check video_grouper/ball_tracking/ video_grouper/api_integrations/ttt_api.py tests/test_ball_tracking_secure_loader.py` → clean
- `uv run pytest tests/test_ball_tracking_secure_loader.py` → 19/19 passing
- `uv run pytest tests/test_ttt_api.py` → 31/31 passing (no regression)

## Cross-repo dependency

This phase consumes the TTT endpoints from Phase 1 (`/api/models/{key}/versions`, `/api/models/{key}/license`)
and the encrypted-artifact format from Phase 2 (binary container with
`(model_key, version, master_key_id)` AAD). The build/license key
derivation contract is enforced by the corresponding TTT-side test in
`backend/tests/test_scripts_package_premium_model.py::TestKeyDerivationContract`.
