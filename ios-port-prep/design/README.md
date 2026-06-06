# soccer-cam-ios design docs

Specs for the iOS / iPadOS port of soccer-cam's ball detection + render
pipeline. Produced on Windows (Phase W.5 of the iOS-port plan) so Mac
sessions can drop straight into Xcode work without re-deriving design.

Read in this order:

1. **`architecture.md`** — Swift module layout, actors, threading, concurrency model
2. **`data_model.md`** — every JSON schema the app reads/writes
3. **`segment_pipeline.md`** — per-segment lifecycle (orchestrates everything below)
4. **`reolink_segment_ingest.md`** — Reolink polling + bulk-import source
5. **`swift_projection_math.md`** — port spec for `cylindrical_view.py`
6. **`swift_kalman_tracker.md`** — port spec for `ball_tracker.py`
7. **`swift_camera_state_machine.md`** — port spec for `render.py`'s state machine
8. **`metal_warp_shader.md`** — Metal compute kernel + host wrapper (replaces OpenCL)
9. **`cryptokit_decryption.md`** — Swift CryptoKit equivalent of `secure_loader.py`
10. **`ttt_api_integration.md`** — TTT auth, model catalog, video upload
11. **`app_ui.md`** — screen-by-screen UI spec
12. **`app_store_plan.md`** — submission strategy, privacy labels, IAP posture

Each Swift port spec includes:

- File / module locations
- Function-by-function mapping from the Python reference
- Parity tolerance (vs the checked-in baselines in `../baselines/`)
- Test scaffold sketches that the Mac sessions flesh out

Cross-references between docs use `[[name]]`-style brackets so the
shared concepts stay traceable as the docs evolve.

## What's NOT in here

Per [[feedback_no_security_docs_in_oss]]:

- Threat models for the model-licensing scheme
- Forensic-watermarking rationale
- Specific TTT model identifiers, sizes, training-data details

Those live in TTT's private docs.

Per [[feedback_no_decrypted_onnx_in_oss]]:

- Specific model filenames or version numbers
- Vendor identifiers
- Enumerations of model types

All references use generic placeholders.
