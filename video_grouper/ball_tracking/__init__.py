"""Model-licensing layer for soccer-cam.

The legacy ball-tracking provider/registry system has been removed in favor of
the config-driven pipeline (:mod:`video_grouper.pipeline`). This package now
exists solely to host the model-licensing modules that the pipeline's detect
step depends on:

* :mod:`video_grouper.ball_tracking.secure_loader`
* :mod:`video_grouper.ball_tracking.license_state`

Keep this package importable for those two modules.
"""
