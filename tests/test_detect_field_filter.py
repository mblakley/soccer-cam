"""Tests for the field-polygon detection filter in DetectStage."""

from __future__ import annotations

import json
from pathlib import Path


from video_grouper.ball_tracking.providers.homegrown.stages.detect import (
    _filter_by_polygon,
)


# Synthetic rectangular field polygon: x in [100, 900], y in [100, 700]
RECT_POLY = [[100, 100], [900, 100], [900, 700], [100, 700]]


def _write_polygon(tmp_path: Path, polygon=None) -> Path:
    payload = {"polygon": polygon if polygon is not None else RECT_POLY}
    p = tmp_path / "field.json"
    p.write_text(json.dumps(payload))
    return p


class TestFilterByPolygon:
    def test_no_polygon_path_passes_through(self):
        dets = [{"cx": 999, "cy": 999, "w": 10, "h": 10, "conf": 0.9}]
        out, dropped = _filter_by_polygon(dets, None, margin_px=0)
        assert out == dets
        assert dropped == 0

    def test_missing_polygon_file_passes_through(self, tmp_path):
        dets = [{"cx": 999, "cy": 999, "w": 10, "h": 10, "conf": 0.9}]
        out, dropped = _filter_by_polygon(
            dets, str(tmp_path / "nope.json"), margin_px=0
        )
        assert out == dets
        assert dropped == 0

    def test_null_polygon_passes_through(self, tmp_path):
        path = tmp_path / "field.json"
        path.write_text(json.dumps({"polygon": None}))
        dets = [{"cx": 999, "cy": 999, "w": 10, "h": 10, "conf": 0.9}]
        out, dropped = _filter_by_polygon(dets, str(path), margin_px=0)
        assert out == dets
        assert dropped == 0

    def test_inside_kept(self, tmp_path):
        path = _write_polygon(tmp_path)
        dets = [{"cx": 500, "cy": 400, "w": 10, "h": 10, "conf": 0.9}]
        out, dropped = _filter_by_polygon(dets, str(path), margin_px=0)
        assert out == dets
        assert dropped == 0

    def test_outside_dropped(self, tmp_path):
        path = _write_polygon(tmp_path)
        dets = [{"cx": 50, "cy": 400, "w": 10, "h": 10, "conf": 0.9}]
        out, dropped = _filter_by_polygon(dets, str(path), margin_px=0)
        assert out == []
        assert dropped == 1

    def test_outside_within_margin_kept(self, tmp_path):
        path = _write_polygon(tmp_path)
        # 30 px above the top edge (y=70 vs y=100), inside 50px margin
        dets = [{"cx": 500, "cy": 70, "w": 10, "h": 10, "conf": 0.9}]
        out, dropped = _filter_by_polygon(dets, str(path), margin_px=50)
        assert len(out) == 1
        assert dropped == 0

    def test_outside_beyond_margin_dropped(self, tmp_path):
        path = _write_polygon(tmp_path)
        # 100 px above top edge, beyond 50px margin
        dets = [{"cx": 500, "cy": -1, "w": 10, "h": 10, "conf": 0.9}]
        out, dropped = _filter_by_polygon(dets, str(path), margin_px=50)
        assert out == []
        assert dropped == 1

    def test_mixed_batch(self, tmp_path):
        path = _write_polygon(tmp_path)
        dets = [
            {"cx": 500, "cy": 400, "w": 10, "h": 10, "conf": 0.9},  # inside
            {"cx": 50, "cy": 400, "w": 10, "h": 10, "conf": 0.9},  # outside
            {"cx": 800, "cy": 600, "w": 10, "h": 10, "conf": 0.9},  # inside
            {"cx": 999, "cy": 999, "w": 10, "h": 10, "conf": 0.9},  # outside
        ]
        out, dropped = _filter_by_polygon(dets, str(path), margin_px=0)
        assert len(out) == 2
        assert dropped == 2
        assert out[0]["cx"] == 500
        assert out[1]["cx"] == 800
