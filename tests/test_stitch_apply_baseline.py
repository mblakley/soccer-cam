"""The double-compose guard in `apply_calibration`.

The bug: the live mesh is only the baseline on an *uncalibrated* unit. Once a
calibration is installed the live mesh is `factory (+) old`, so composing onto
it gives `factory (+) old (+) new` -- which the next boot silently rewrites as
`factory (+) new`, because `S98_StitchCal` composes from the saved factory copy.
Same stored anchors, two different meshes, distinguished only by whether the
unit has been power-cycled.

These tests pin the four cases that matter: onto factory, onto our own
correction (must not double), when something else moved the mesh (must still
refuse), and idempotency.

Transport is faked throughout; the live end-to-end run is recorded in
STITCH_CALIBRATION.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

VPE = Path(__file__).resolve().parents[1] / "reolink-firmware-patching" / "vpe"
if str(VPE) not in sys.path:
    sys.path.insert(0, str(VPE))

lut2d = pytest.importorskip("lut2d")
stitch_apply = pytest.importorskip("stitch_apply")

ANCHORS_A = [(0.0, 4.0), (2159.0, 4.0)]
ANCHORS_B = [(0.0, 8.0), (2159.0, 8.0)]


def _factory(n: int = 33):
    return lut2d.Lut2D.identity(n, lut2d.DEFAULT_HALF_WIDTH, lut2d.DEFAULT_HALF_HEIGHT)


def _composed(baseline, anchors):
    text = lut2d.format_anchors(anchors, baseline_crc32=lut2d.crc32(baseline))
    mesh, _stats = lut2d.compose_from_anchors_file(baseline, text)
    return mesh


class FakeCamera:
    """A camera whose mesh actually changes when the helper composes and sets.

    Faithful enough to catch doubling: `set` really does replace the live mesh
    with the composed one, so an apply that composes from the wrong baseline
    produces a visibly different crc.
    """

    def __init__(self, factory):
        self.factory = factory
        self.live = factory
        self.files: dict[str, bytes] = {}
        self.text: dict[str, str] = {}
        self.set_calls = 0
        self.foreign_change_before_write = None

    # -- the surfaces stitch_apply uses ---------------------------------
    def dump_mesh(self, host, vpe_id=0, name="baseline.bin"):
        # A foreign change lands between the baseline dump and the write, which
        # is exactly the window the re-check exists to cover.
        if name == "recheck.bin" and self.foreign_change_before_write is not None:
            self.live = self.foreign_change_before_write
            self.foreign_change_before_write = None
        self.files[name] = self.live.to_bytes()
        return "wrote"

    def read_mesh(self, host, name="baseline.bin"):
        return lut2d.Lut2D.from_bytes(self.files[name])

    def fetch_optional(self, host, sd_relative):
        key = sd_relative.split("/")[-1]
        if key in self.files:
            return self.files[key]
        if key in self.text:
            return self.text[key].encode()
        return None

    def push_text(self, host, remote_path, text):
        self.text[remote_path.split("/")[-1]] = text

    def get_stitch(self, host, user, password):
        s = stitch_apply.Scalars(1.0, 0, 0)
        return s, s

    def sh(self, cmd, host=None, timeout=None, **kw):
        if "-x" in cmd and "lut2d_ioctl" in cmd and "compose" not in cmd:
            return "BAKED"  # _helper() probe
        if "compose" not in cmd:
            return ""
        if "cp factory_boot.bin baseline.bin" in cmd:
            self.files["baseline.bin"] = self.files["factory_boot.bin"]
        elif "cp baseline.bin factory_boot.bin" in cmd:
            self.files["factory_boot.bin"] = self.files["baseline.bin"]

        baseline = lut2d.Lut2D.from_bytes(self.files["baseline.bin"])
        mesh, stats = lut2d.compose_from_anchors_file(
            baseline, self.text["anchors.txt"], require_baseline=True
        )
        self.live = mesh
        self.files["mesh_apply.bin"] = mesh.to_bytes()
        self.set_calls += 1
        if "applied.sig" in cmd:
            self.text["applied.sig"] = stitch_apply.mesh_signature(mesh.to_bytes())
        return f"read-back matches, crc {stats.result_crc32:08x}"


@pytest.fixture
def cam(monkeypatch):
    c = FakeCamera(_factory())
    monkeypatch.setattr(stitch_apply, "dump_mesh", c.dump_mesh)
    monkeypatch.setattr(stitch_apply, "read_mesh", c.read_mesh)
    monkeypatch.setattr(stitch_apply, "_fetch_optional", c.fetch_optional)
    monkeypatch.setattr(stitch_apply, "push_text", c.push_text)
    monkeypatch.setattr(stitch_apply, "get_stitch", c.get_stitch)
    monkeypatch.setattr(stitch_apply, "sh", c.sh)
    monkeypatch.setattr(
        stitch_apply, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})
    )
    return c


def _apply(anchors, **kw):
    return stitch_apply.apply_calibration(anchors, password="x", **kw)


# -- the four cases ----------------------------------------------------------


def test_apply_onto_factory_uses_the_live_mesh_as_baseline(cam):
    report = _apply(ANCHORS_A)
    assert report["baseline"]["source"] == "live mesh"
    assert report["baseline"]["our_correction_was_live"] is False
    assert lut2d.crc32(cam.live) == lut2d.crc32(_composed(cam.factory, ANCHORS_A))


def test_apply_onto_our_own_correction_does_not_double(cam):
    _apply(ANCHORS_A)
    first = lut2d.crc32(cam.live)
    report = _apply(ANCHORS_B)
    assert report["baseline"]["source"] == "saved factory copy"
    assert report["baseline"]["our_correction_was_live"] is True
    want = lut2d.crc32(_composed(cam.factory, ANCHORS_B))
    doubled = lut2d.crc32(_composed(_composed(cam.factory, ANCHORS_A), ANCHORS_B))
    assert lut2d.crc32(cam.live) == want, "must be factory (+) B"
    assert lut2d.crc32(cam.live) != doubled, "must NOT be factory (+) A (+) B"
    assert first != want


def test_applying_the_same_anchors_twice_is_idempotent(cam):
    _apply(ANCHORS_A)
    once = lut2d.crc32(cam.live)
    _apply(ANCHORS_A)
    assert lut2d.crc32(cam.live) == once


def test_refuses_when_something_else_moved_the_mesh(cam):
    other = _factory()
    other.set(0, 0, 11.0, 11.0)
    cam.foreign_change_before_write = other
    # the guard fires on the re-check, before any write
    with pytest.raises(stitch_apply.OrderingViolation):
        _apply(ANCHORS_A)


def test_a_foreign_mesh_becomes_the_new_baseline_next_time(cam):
    """Self-healing: a legitimate SetStitch produces a mesh matching neither our
    signature nor the saved copy, and must be adopted as the baseline."""
    _apply(ANCHORS_A)
    resected = _factory()
    resected.set(1, 1, 9.0, 9.0)
    cam.live = resected  # as if SetStitch re-ran the optimiser
    report = _apply(ANCHORS_B)
    assert report["baseline"]["source"] == "live mesh"
    assert lut2d.crc32(cam.live) == lut2d.crc32(_composed(resected, ANCHORS_B))


# -- the pieces the two paths share ------------------------------------------


def test_signature_skips_the_header_so_dumps_and_composed_files_compare(cam):
    """A driver dump and a composed file carry different 8-byte headers; only
    the table is comparable, which is why S98's mesh_sig skips it."""
    mesh = _composed(_factory(), ANCHORS_A)
    blob = mesh.to_bytes()
    other_header = b"\xff" * 8 + blob[8:]
    assert stitch_apply.mesh_signature(blob) == stitch_apply.mesh_signature(
        other_header
    )


def test_signature_byte_range_matches_the_boot_hook():
    """`S98_StitchCal` uses `dd bs=8 skip=1`; drift here silently reintroduces
    the doubling, because the two paths would stop recognising each other."""
    hook = (VPE.parent / "runtime" / "stitchcal" / "S98_StitchCal").read_text()
    assert 'dd if="$1" bs=8 skip=1' in hook
    assert stitch_apply.MESH_SIG_SKIP == 8


def test_applied_sig_is_written_so_the_boot_path_agrees(cam):
    _apply(ANCHORS_A)
    assert cam.text["applied.sig"] == stitch_apply.mesh_signature(cam.live.to_bytes())


def test_anchors_are_stamped_with_the_true_baseline_not_the_live_mesh(cam):
    _apply(ANCHORS_A)
    _apply(ANCHORS_B)
    _parsed, meta = lut2d.parse_anchors(cam.text["anchors.txt"])
    assert meta["baseline_crc32"] == lut2d.crc32(cam.factory)


def test_dry_run_writes_nothing(cam):
    _apply(ANCHORS_A, dry_run=True)
    assert cam.set_calls == 0
    assert lut2d.crc32(cam.live) == lut2d.crc32(cam.factory)
