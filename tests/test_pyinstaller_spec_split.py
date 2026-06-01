"""Guard the tray/service PyInstaller exclude split.

The tray bundle drives only the autocam (GUI) step and must stay light, so it
EXCLUDES the inference stack (onnxruntime / cv2 / av). The service bundle runs
detect/render in Session 0 and must RETAIN them. These string checks fail loud
if either spec's excludes regress (the alternative — building both bundles — is
far too slow for unit tests).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_INFERENCE_STACK = ("onnxruntime", "cv2", "av")


def _excludes(spec_path: Path) -> str:
    """Return the full contents of the `excludes=[...]` list.

    Captures across newlines so a multi-line list (PyInstaller may reformat it)
    can't make these checks silently false-pass / false-fail.
    """
    text = spec_path.read_text(encoding="utf-8")
    m = re.search(r"excludes\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not m:
        raise AssertionError(f"no excludes=[...] found in {spec_path}")
    return m.group(1)


@pytest.mark.parametrize("module", _INFERENCE_STACK)
def test_tray_spec_excludes_inference_stack(module):
    excludes = _excludes(_ROOT / "VideoGrouperTray.spec")
    assert f"'{module}'" in excludes, (
        f"VideoGrouperTray.spec must exclude {module!r} (tray drives only the "
        f"autocam GUI step); excludes: {excludes}"
    )


@pytest.mark.parametrize("module", _INFERENCE_STACK)
def test_service_spec_retains_inference_stack(module):
    excludes = _excludes(_ROOT / "VideoGrouperService.spec")
    assert f"'{module}'" not in excludes, (
        f"VideoGrouperService.spec must NOT exclude {module!r} (service runs "
        f"detect/render in Session 0); excludes: {excludes}"
    )


def test_both_specs_still_exclude_heavy_ml_libs():
    """Both bundles keep the existing torch/scipy excludes — the split adds to
    the tray's excludes, it doesn't remove anything from either."""
    for spec in ("VideoGrouperTray.spec", "VideoGrouperService.spec"):
        excludes = _excludes(_ROOT / spec)
        for module in ("torch", "torchvision", "ultralytics", "scipy"):
            assert f"'{module}'" in excludes, f"{spec} dropped {module!r}: {excludes}"
