# Phase 5 — Cython Hardening (production gate)

Branch: `feature/ball-detection-loader` (continuing on Phase 3 branch)
Status: STRUCTURE COMPLETE, COMPILE-IN-CI PENDING

## Goal

Move the security-critical helpers in `secure_loader.py` (canonical JSON, AAD
construction, license verification, AES-GCM decrypt orchestration) into a
Cython-compiled native extension so the on-disk artifact is a `.pyd` /
`.so` / `.dylib` rather than introspectable Python bytecode. Decompilation of
installed soccer-cam should not recover the security glue.

## What landed

### `video_grouper/ball_tracking/_secure_loader_native.pyx`

Cython source mirroring the security helpers in `secure_loader.py`:
`canonical_json`, `parse_expires_at`, `verify_signature`, `verify_license`,
`parse_artifact`, `build_aad`, `decrypt_artifact`. Each is a `cpdef`
function with typed args. Errors raise `NativeSecureLoaderError`, which the
wrapper translates to `SecureLoaderError` for the public API.

### `secure_loader.py` — try-native, fall-back-Python

Top-of-module import attempt:

```python
try:
    from video_grouper.ball_tracking import _secure_loader_native as _native
    _NATIVE_AVAILABLE = True
except ImportError:
    _native = None
    _NATIVE_AVAILABLE = False
```

Each helper checks `_NATIVE_AVAILABLE`. If true → call into the compiled
module. If false → run the pure-Python implementation (unchanged from
Phase 3). Native errors are translated through `_translate_native_error`.

Result: dev / unit tests / installations without a C toolchain run pure
Python; release builds run the compiled module. Same behavior, same
public API, same test coverage.

### `build_native_loader.py`

Setuptools + cython build script. CI runs:

```
uv pip install cython setuptools cryptography
python build_native_loader.py build_ext --inplace
```

…producing `_secure_loader_native*.{pyd,so,dylib}` next to the source.

### `.github/workflows/build-native-loader.yml`

Cross-platform matrix: Windows x64 (windows-latest, MSVC), macOS arm64
(macos-14, clang), Linux x64 (ubuntu-latest, clang). Uploads each
compiled artifact for downstream release jobs. Triggers on changes to
the .pyx source or this workflow. Acceptance step verifies the
compiled extension exists, has a non-trivial size, and imports cleanly.

### `pyproject.toml`

Added `cython>=3.0.0` and `setuptools>=70.0.0` to the `[dev]` extras so
contributors can build the native module locally. Production
dependencies are unchanged.

## Acceptance criteria — what's met, what's pending

| Criterion | Status |
|---|---|
| Security-critical paths exist in Cython source | ✅ |
| `secure_loader.py` delegates to native when present | ✅ |
| Pure-Python fallback works for dev/test | ✅ (19/19 tests passing) |
| Cross-platform build wired up in CI | ✅ workflow file written |
| Compiled artifact verified to load on each platform | ⏳ first CI run |
| Decompilation does not recover security logic | ⏳ requires actual compile |
| Self-integrity check: tamper detection on the compiled .pyd | ⏳ deferred |
| `strings` reveals no AES constants or readable function names | ⏳ requires actual compile |

The "⏳" rows depend on CI running the workflow. The structure is in
place; running `build-native-loader.yml` against the matrix delivers the
remaining acceptance.

## Out of scope (deferred)

- **Authenticode signing of the .pyd on Windows** — adds operational
  complexity (signing key custody, signtool setup). Important before
  shipping a customer-facing build but separable from the Cython work.
- **codesign on macOS** — same.
- **Self-integrity check at startup** — would compute the hash of the
  loaded `_secure_loader_native` and compare to a value baked into a
  Python-side constant. Layered defense; deferred.
- **Stripped debug symbols on release builds** — easy to add to the
  build script but yields little once Cython has done its compile pass.

## Verification (today)

- `uv run ruff check video_grouper/ball_tracking/` → clean
- `uv run pytest tests/test_ball_tracking_secure_loader.py` → 19/19 passing
  (pure-Python fallback path)

## Verification (in CI on next run)

- `build-native-loader.yml` compiles on Windows x64, macOS arm64, Linux x64
- Each compiled artifact loads + has size > 50 KB
- Test suite runs against the compiled module (separate test job that
  pip-installs from the produced artifacts) — to be added when the first
  CI run lands and we know the artifact paths
