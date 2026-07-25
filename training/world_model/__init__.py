"""Physics- and game-aware ball world-model (research track).

A Bayesian state-space estimator over the single game ball. Learned detectors,
motion and saliency are *measurements*; physics + field geometry + game context
are *dynamics and priors*; track-before-detect is the inference engine.

This package is a parallel, different-angle research track to the per-frame
heatmap detector on ``feat/perspective-normalized-detector``. It treats object
detection as one noisy measurement feeding a world-model that knows where the
ball can physically be.

Modules:
    geometry: zero-touch field geometry — ground-plane homography from the
        auto-detected 10-point field polygon, a geometric ball-size(location)
        prior, and the field + 3-D dome support region. Pure numpy + cv2.
"""

from __future__ import annotations
