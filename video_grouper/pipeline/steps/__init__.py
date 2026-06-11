"""Built-in pipeline steps.

Don't import these modules directly for their side effects — go through
:mod:`video_grouper.pipeline.register_steps`, which wraps each import in a
try/except so a missing optional dependency in a given bundle (e.g. no
``onnxruntime`` in the tray bundle) silently omits that one step instead of
poisoning the whole registry.
"""
