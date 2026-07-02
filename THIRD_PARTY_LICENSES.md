# Third-party licenses

soccer-cam is licensed under GPL-3.0 (see [`LICENSE`](./LICENSE)). It bundles the
following separately-licensed third-party components. Their licenses are
reproduced alongside the component and listed here.

## Bundled model weights

### Ultralytics YOLO26n — `video_grouper/models/person.onnx`

- **Component:** stock Ultralytics YOLO26n person detector, exported to ONNX,
  bundled so game-phase detection works without a separate model download.
- **License:** **AGPL-3.0** — full text at
  [`video_grouper/models/LICENSE-AGPL-3.0.txt`](./video_grouper/models/LICENSE-AGPL-3.0.txt).
- **Copyright:** © Ultralytics — https://github.com/ultralytics/ultralytics
- **Notes:** AGPL-3.0 is compatible with soccer-cam's GPL-3.0. The model is used
  for local inference on the camera manager's own machine (no remote/hosted
  service). Provenance, hash, and regeneration steps are in
  [`video_grouper/models/README.md`](./video_grouper/models/README.md). These
  weights must remain on the soccer-cam (open-source) side only.
