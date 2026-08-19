"""Apply a stitch calibration to the camera, in the one order that is correct.

THE ORDERING CONSTRAINT, AND WHY THIS FILE EXISTS.

`SetStitch` does not merely store three numbers. It feeds `Na_calc_2dlut_data`,
the firmware's own iterative mesh optimiser, which regenerates the VPE 0 warp
mesh from scratch -- **destroying any mesh we previously wrote**. So the vendor
scalars must be applied *before* the mesh, always, and a mesh composed against a
pre-`SetStitch` baseline is simply wrong.

That is easy to write down and easy to get wrong at 1 a.m., so it is not left to
a document. `apply_calibration` owns the whole sequence and there is no public
entry point that writes a mesh on its own:

    1. set scalars (if any)      -- HTTP, persists to /mnt/para/stitch.cfg
    2. wait for the pipeline to settle and the mesh to stop changing
    3. decide the TRUE baseline: the live mesh, unless the live mesh is our
       own previous correction, in which case it is the saved factory copy
    4. compose the correction onto THAT
    5. write, with read-back verification, and record what we wrote
    6. re-dump and confirm nothing else moved the mesh under us

Step 6 is the guard that makes step 1 unskippable in practice: if anything
re-ran the optimiser between the baseline dump and the write -- another
operator, the app, a second copy of this tool -- the composed mesh no longer
matches its baseline and the write is refused rather than silently applied on
top of a different calibration.

Step 3 is not a formality. The live mesh is only the baseline on an
uncalibrated unit; once a calibration is installed it is `factory (+) old`, and
composing onto it gives `factory (+) old (+) new` -- which the next boot then
rewrites as `factory (+) new`, because the boot hook composes from the saved
factory copy. Same stored anchors, two different meshes, distinguished only by
whether the unit has been power-cycled. `S98_StitchCal` avoids this by keeping
`applied.sig`, and this file uses the same file, the same byte range and the
same rule, so the two paths cannot drift apart. That matters most for the
sequence the editor now encourages: load the installed calibration, nudge it,
apply.

The boot hook satisfies the scalars constraint structurally instead: it composes
onto whatever mesh the firmware generated at boot, which by construction already
reflects the persisted scalars.

Transport: HTTP for the scalars and the snapshot, the port-2323 probe shell for
everything else, `wget` for pushing files (there is no base64 on the device).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

from camsh import sh  # noqa: E402
from lut2d import (  # noqa: E402
    DEFAULT_HALF_HEIGHT,
    DEFAULT_HALF_WIDTH,
    DEFAULT_PANORAMA_WIDTH,
    FRAC_SCALE,
    Lut2D,
    Lut2DError,
    compose_from_anchors_file,
    crc32,
    format_anchors,
    interp_dx,
    parse_anchors,
)

CAM_DIR = "/mnt/sda/stitchcal"
DEFAULT_HOST = "192.168.86.24"


class OrderingViolation(Exception):
    """The mesh was about to be written against a baseline that had moved."""


@dataclass
class Scalars:
    distance: float
    stitchXMove: int
    stitchYMove: int

    @classmethod
    def from_api(cls, d: dict) -> Scalars:
        return cls(float(d["distance"]), int(d["stitchXMove"]), int(d["stitchYMove"]))

    def to_api(self) -> dict:
        return {
            "distance": self.distance,
            "stitchXMove": self.stitchXMove,
            "stitchYMove": self.stitchYMove,
        }


# -- HTTP surface -------------------------------------------------------------


def _api(host: str, user: str, password: str, cmd: str, body: list) -> list:
    qs = urllib.parse.urlencode({"cmd": cmd, "user": user, "password": password})
    req = urllib.request.Request(
        f"http://{host}/cgi-bin/api.cgi?{qs}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return json.loads(r.read().decode())


def get_stitch(host: str, user: str, password: str) -> tuple[Scalars, Scalars]:
    """Return (current, factory). The camera holds its own factory baseline."""
    rsp = _api(
        host,
        user,
        password,
        "GetStitch",
        [{"cmd": "GetStitch", "action": 1, "param": {"channel": 0}}],
    )
    v = rsp[0]["value"]["stitch"]
    i = rsp[0].get("initial", {}).get("stitch", v)
    return Scalars.from_api(v), Scalars.from_api(i)


def set_stitch(host: str, user: str, password: str, s: Scalars) -> None:
    rsp = _api(
        host,
        user,
        password,
        "SetStitch",
        [{"cmd": "SetStitch", "action": 0, "param": {"stitch": s.to_api()}}],
    )
    code = rsp[0].get("value", {}).get("rspCode", rsp[0].get("code"))
    if code not in (200, 0):
        raise RuntimeError(f"SetStitch refused: {rsp}")


def snap(host: str, user: str, password: str, out: Path) -> Path:
    qs = urllib.parse.urlencode(
        {
            "cmd": "Snap",
            "channel": 0,
            "rs": "stitchcal",
            "user": user,
            "password": password,
        }
    )
    with urllib.request.urlopen(  # noqa: S310
        f"http://{host}/cgi-bin/api.cgi?{qs}", timeout=60
    ) as r:
        out.write_bytes(r.read())
    return out


def fetch_sd(host: str, sd_relative: str, out: Path) -> Path:
    """Pull a file off the SD card over the /downloadfile/ HTTP unlock."""
    with urllib.request.urlopen(  # noqa: S310
        f"http://{host}/downloadfile/{sd_relative}", timeout=120
    ) as r:
        out.write_bytes(r.read())
    return out


# -- shell surface ------------------------------------------------------------


def _helper(host: str) -> str:
    """Path to lut2d_ioctl on the camera.

    Baked into the firmware at /usr/bin by the stitchcal build; an SD-card copy
    wins so the helper can be iterated on without a reflash. Same precedence as
    S98_StitchCal uses, so the interactive path and the boot path always run the
    same binary.
    """
    sd = f"{CAM_DIR}/bin/lut2d_ioctl"
    out = sh(f"[ -x {sd} ] && echo SD || echo BAKED", host=host)
    return sd if "SD" in out else "/usr/bin/lut2d_ioctl"


def dump_mesh(host: str, vpe_id: int = 0, name: str = "baseline.bin") -> str:
    out = sh(
        f"mkdir -p {CAM_DIR} && rm -f {CAM_DIR}/{name} && "
        f"{_helper(host)} get {vpe_id} {CAM_DIR}/{name} 2>&1",
        host=host,
        timeout=90,
    )
    if "wrote" not in out:
        raise RuntimeError(f"mesh dump failed:\n{out}")
    return out


def read_mesh(host: str, name: str = "baseline.bin") -> Lut2D:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = fetch_sd(host, f"stitchcal/{name}", Path(td) / name)
        return Lut2D.from_bytes(p.read_bytes())


def wait_for_stable_mesh(host: str, tries: int = 12, interval: float = 5.0) -> Lut2D:
    """Poll the live mesh until two consecutive reads agree.

    `SetStitch` returns before the optimiser has finished reprogramming the
    DCE, so 'wait for the pipeline to settle' has to be a condition and not a
    sleep -- a fixed sleep is how you end up composing onto a half-written
    baseline exactly once, on the day it matters.
    """
    prev: int | None = None
    for _ in range(tries):
        dump_mesh(host, name="baseline.bin")
        lut = read_mesh(host, "baseline.bin")
        cur = crc32(lut)
        if prev is not None and cur == prev:
            return lut
        prev = cur
        time.sleep(interval)
    raise RuntimeError(
        f"the live mesh never stopped changing after {tries} reads -- refusing "
        "to compose onto a moving baseline"
    )


# -- reading what is already on the camera ------------------------------------
#
# The editor has to start from the camera's real state rather than from a flat
# zero curve, or the operator's first adjustment is a delta from nothing.
#
# One thing this deliberately does NOT do: apply the mesh to the snapshot.
# `cmd=Snap` already returns the *fused* 7680x2160 panorama -- the VPE warp and
# the stitcher have both run by the time the JPEG exists, which is precisely why
# the blend corridor and its ghost are visible in it at all. Warping that image
# by the mesh again would apply the correction twice. What the mesh can honestly
# supply is its own *shape*, which is what `seam_profile` below extracts.

#: Where the boot hook leaves this boot's dumped factory mesh. Both names are
#: present on the unit and byte-identical (verified live 2026-08-19, md5
#: 7ac18ef2988970d09fc4f5a36c6cd311); `factory_boot.bin` is the one
#: `S98_StitchCal` writes and documents, so it is tried first.
FACTORY_COPIES = ("factory_boot.bin", "factory_vpe0.bin")

#: Bytes skipped when identifying a mesh by its table alone.
#:
#: Must stay in lockstep with `S98_StitchCal`'s `mesh_sig()`, which is
#: `dd bs=8 skip=1 | md5sum`: the interactive path writes `applied.sig` and the
#: boot path reads it, so a disagreement here reintroduces exactly the doubling
#: this exists to prevent. The header is skipped because a driver dump carries
#: {id, n} plus whatever the driver left in the third word, while a composed
#: file carries a canonical header -- only the table is comparable.
MESH_SIG_SKIP = 8


def mesh_signature(blob: bytes) -> str:
    """Identify a mesh by its table alone, exactly as `S98_StitchCal` does."""
    return hashlib.md5(blob[MESH_SIG_SKIP:]).hexdigest()  # noqa: S324


def seam_profile(
    lut: Lut2D,
    *,
    dst_width: float = DEFAULT_HALF_WIDTH,
    dst_height: float = DEFAULT_HALF_HEIGHT,
) -> list[dict]:
    """Per-row behaviour of a mesh at the seam column: the vendor's own solution.

    The mesh maps destination grid points to *source* pixels, and the seam is
    the left half's last column, so `src_x - dst_x` there is the horizontal
    displacement the camera's optimiser chose for that row. `s` is the local
    source-pixels-per-destination-pixel, computed exactly as
    `compose_correction` computes it, because that is the factor an operator's
    `dx` gets multiplied by: the composer writes `x + dx*s`. Showing both means
    the operator can see the shape the camera chose *and* how far a given
    correction will actually move things on each row.
    """
    n = lut.n
    if n < 3:
        raise Lut2DError(f"mesh too small to profile: n={n}")
    du = (dst_width - 1.0) / (n - 1)
    dv = (dst_height - 1.0) / (n - 1)
    seam_col = n - 1
    dst_x = seam_col * du
    out: list[dict] = []
    for row in range(n):
        base = row * lut.stride
        x_last = (lut.entries[base + seam_col] & 0xFFFF) / FRAC_SCALE
        x_prev = (lut.entries[base + seam_col - 1] & 0xFFFF) / FRAC_SCALE
        out.append(
            {
                "row": row,
                "y": round(row * dv, 1),
                "src_x": round(x_last, 3),
                "offset_px": round(x_last - dst_x, 3),
                "s": round((x_last - x_prev) / du, 5),
            }
        )
    return out


def _fetch_optional(host: str, sd_relative: str) -> bytes | None:
    """Read a file off the SD card, or None if the camera does not have it.

    A 404 here is information, not an error: no `anchors.txt` means this unit
    has never been calibrated, which is a state the UI must state plainly.
    """
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as td:
            p = fetch_sd(host, sd_relative, Path(td) / "f")
            return p.read_bytes()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except urllib.error.URLError:
        return None


def _fetch_text(host: str, sd_relative: str) -> str:
    """A small text file off the SD card, or "" if it is not there."""
    blob = _fetch_optional(host, sd_relative)
    return "" if blob is None else blob.decode("utf-8", "replace").strip()


@dataclass
class CameraCalibration:
    """What is actually installed on the camera right now."""

    live_crc32: int = 0
    factory_crc32: int | None = None
    factory_name: str = ""
    at_factory: bool | None = None
    anchors: list[tuple[float, float]] | None = None
    anchors_meta: dict | None = None
    anchors_error: str = ""
    profile: list[dict] | None = None
    note: str = ""

    def anchors_at(
        self,
        rows: Sequence[float],
        *,
        src_width: float = DEFAULT_PANORAMA_WIDTH,
        src_height: float = DEFAULT_HALF_HEIGHT,
    ) -> list[tuple[float, float]] | None:
        """The installed curve resampled onto `rows`, or None if uncalibrated.

        Note the geometry: a "half" is half in *width* only, so the panorama
        and the half share a height of `DEFAULT_HALF_HEIGHT`. Treating that
        constant as a half-height silently halves every row.

        `anchors.txt` records the geometry it was authored against, and the
        editor works in the geometry of the frame currently loaded. Those are
        both 7680x2160 in the shipping setup, but a calibration recovered from a
        downscaled still is exactly the case that would otherwise apply a
        proportionally wrong curve with nothing complaining.
        """
        if not self.anchors:
            return None
        meta = self.anchors_meta or {}
        file_h = float(meta.get("src_height") or src_height)
        file_w = float(meta.get("src_width") or src_width)
        y_scale = file_h / src_height if src_height else 1.0
        dx_scale = src_width / file_w if file_w else 1.0
        return [
            (float(y), interp_dx(self.anchors, y * y_scale) * dx_scale) for y in rows
        ]

    def to_api(self) -> dict:
        return {
            "live_crc32": f"{self.live_crc32:08x}",
            "factory_crc32": (
                None if self.factory_crc32 is None else f"{self.factory_crc32:08x}"
            ),
            "factory_name": self.factory_name,
            "at_factory": self.at_factory,
            "anchors": (
                None if self.anchors is None else [list(a) for a in self.anchors]
            ),
            "anchors_meta": self.anchors_meta,
            "anchors_error": self.anchors_error,
            "profile": self.profile,
            "note": self.note,
        }


def read_calibration(
    host: str = DEFAULT_HOST, *, with_profile: bool = True
) -> CameraCalibration:
    """Read the camera's current calibration state. Read-only.

    Dumping the mesh is a read of the live VPE state -- it writes only a file on
    the SD card and never touches the VPE or flash. Nothing here calls
    `set_stitch` or `lut2d_ioctl set`.
    """
    dump_mesh(host, name="baseline.bin")
    live = read_mesh(host, "baseline.bin")
    state = CameraCalibration(live_crc32=crc32(live))
    if with_profile:
        state.profile = seam_profile(live)

    for name in FACTORY_COPIES:
        blob = _fetch_optional(host, f"stitchcal/{name}")
        if blob is None:
            continue
        try:
            state.factory_crc32 = crc32(Lut2D.from_bytes(blob))
        except Lut2DError as exc:
            state.anchors_error = f"{name} is unreadable: {exc}"
            continue
        state.factory_name = name
        state.at_factory = state.factory_crc32 == state.live_crc32
        break

    raw = _fetch_optional(host, "stitchcal/anchors.txt")
    if raw is not None:
        try:
            anchors, meta = parse_anchors(raw.decode("utf-8", "replace"))
            state.anchors, state.anchors_meta = anchors, meta
        except Lut2DError as exc:
            state.anchors_error = f"anchors.txt is unparseable: {exc}"

    if state.anchors is None and state.at_factory:
        state.note = (
            "This unit is at factory: no anchors.txt is installed and the live "
            "mesh is byte-identical to the factory copy. Nothing has been applied."
        )
    elif state.anchors is None and state.at_factory is False:
        state.note = (
            "No anchors.txt is installed, but the live mesh differs from the "
            "factory copy. Something changed the mesh outside this tool."
        )
    elif state.anchors is not None:
        # The boot hook composes anchors onto the mesh the firmware just
        # generated, so anchors are always relative to *factory*. The
        # interactive apply path composes onto the *live* mesh instead, so
        # re-applying while a calibration is installed stacks on top of it
        # until the next reboot re-composes from factory. Say so rather than
        # letting an operator discover it.
        state.note = (
            f"{len(state.anchors)} anchors are installed"
            f"{' (' + state.anchors_meta['calibration_id'] + ')' if state.anchors_meta and state.anchors_meta.get('calibration_id') else ''}"
            ". Anchors are relative to the factory mesh, which is what the boot "
            "hook composes against. Applying again from here replaces this "
            "curve at the next boot, but the immediate on-camera result is "
            "composed onto the live mesh -- reboot to make the two agree."
        )
    return state


# -- the ordered sequence -----------------------------------------------------


def apply_calibration(
    anchors: list[tuple[float, float]],
    *,
    host: str = DEFAULT_HOST,
    user: str = "admin",
    password: str,
    scalars: Scalars | None = None,
    calibration_id: str = "",
    install_boot_config: bool = True,
    dry_run: bool = False,
) -> dict:
    """Scalars, then mesh. Returns the `stages[]` witness for the artifact.

    `scalars=None` leaves the vendor settings alone; the ordering guard still
    runs, because something *else* may have moved them.
    """
    report: dict = {"host": host, "stages": []}

    # 1) scalars first -- they regenerate the mesh, so nothing composed before
    #    this point can survive.
    current, factory = get_stitch(host, user, password)
    stage = {
        "surface": "camera_scalars",
        "factory": factory.to_api(),
        "values": current.to_api(),
        "state": "baseline",
    }
    if scalars is not None and scalars != current:
        if dry_run:
            stage["state"] = "would_set"
        else:
            set_stitch(host, user, password, scalars)
            stage["values"] = scalars.to_api()
            stage["state"] = "applied"
    report["stages"].append(stage)

    # 2+3) settle, then work out what the TRUE baseline is.
    #
    # The live mesh is not automatically the baseline. On a unit that already
    # carries a calibration it is `factory (+) old`, and composing onto it
    # yields `factory (+) old (+) new` -- which then silently becomes
    # `factory (+) new` at the next boot, because `S98_StitchCal` composes from
    # the saved factory copy. Two different meshes for the same stored anchors,
    # differing only by whether the unit has been power-cycled.
    #
    # `S98_StitchCal` already solves this and this mirrors it exactly rather
    # than inventing a second scheme: remember the signature of what we last
    # wrote, and if the live mesh IS that, the true baseline is the saved copy.
    # Otherwise the live mesh is the baseline and the saved copy is stale --
    # which is also what makes it self-heal through a legitimate `SetStitch`.
    live = wait_for_stable_mesh(host)
    live_sig = mesh_signature(live.to_bytes())
    prev_sig = _fetch_text(host, "stitchcal/applied.sig")
    factory_blob = _fetch_optional(host, f"stitchcal/{FACTORY_COPIES[0]}")
    ours_is_live = bool(prev_sig) and prev_sig == live_sig and factory_blob is not None

    if ours_is_live:
        baseline = Lut2D.from_bytes(factory_blob)  # type: ignore[arg-type]
        baseline_src = FACTORY_COPIES[0]
    else:
        baseline = live
        baseline_src = ""  # the live dump is already at baseline.bin
    baseline_crc = crc32(baseline)
    report["baseline_crc32"] = f"{baseline_crc:08x}"
    report["baseline"] = {
        "source": "saved factory copy" if ours_is_live else "live mesh",
        "live_sig": live_sig,
        "applied_sig": prev_sig or None,
        "our_correction_was_live": ours_is_live,
    }

    # 4) compose against it
    text = format_anchors(
        anchors, calibration_id=calibration_id, baseline_crc32=baseline_crc
    )
    mesh, stats = compose_from_anchors_file(baseline, text, require_baseline=True)
    report["compose"] = {
        "dx_px": [stats.dx_min_px, stats.dx_max_px],
        "max_src_disp_px": round(stats.max_src_disp_px, 3),
        "s_range": [round(stats.s_min, 4), round(stats.s_max, 4)],
        "s_at_seam": round(stats.s_at_seam, 4),
        "clamped": stats.clamped_low + stats.clamped_high,
        "monotonic_x": round(stats.monotonic_x, 5),
        "result_crc32": f"{stats.result_crc32:08x}",
    }

    # 6) re-check before writing. This is the ordering guard: if anything
    #    re-ran the optimiser since step 3, the mesh we hold was composed
    #    against a calibration that no longer exists.
    #
    #    Compare against the LIVE signature seen at decision time, not against
    #    the baseline crc. Those are the same thing only on an uncalibrated
    #    unit; once our own correction is live they differ by exactly that
    #    correction, and a baseline comparison would refuse every re-calibration
    #    while still failing to catch the doubling it was supposed to prevent.
    #    What must be detected is *someone else* moving the mesh, which is a
    #    change away from the signature we just observed.
    dump_mesh(host, name="recheck.bin")
    if mesh_signature(read_mesh(host, "recheck.bin").to_bytes()) != live_sig:
        raise OrderingViolation(
            "the live mesh changed between the baseline dump and the write. "
            "Something re-ran SetStitch (or another copy of this tool is "
            "running). Refusing to write a mesh composed against a baseline "
            "that no longer exists -- start over."
        )

    if dry_run:
        report["stages"].append({"surface": "camera_mesh", "state": "dry_run"})
        return report

    # 5) Compose and write ON THE CAMERA, with the same helper the boot hook
    #    uses. The mesh validated now is then bit-for-bit the mesh restored at
    #    every subsequent boot -- if this step composed off-camera and pushed
    #    the result, the two paths could diverge without anyone noticing.
    #    `--require-baseline` is set here and deliberately not at boot: this is
    #    the interactive path, where a baseline that moved means the operator
    #    changed something mid-flight.
    push_text(host, f"{CAM_DIR}/anchors.txt", text)
    helper = _helper(host)
    # Put the true baseline where the camera-side composer reads it, and keep
    # the saved factory copy current, in the same order `S98_StitchCal` does:
    # either restore the factory copy as the compose input (our correction was
    # live), or save the live mesh as the factory copy (it is the baseline).
    stage_baseline = (
        f"cp {baseline_src} baseline.bin"
        if baseline_src
        else f"cp baseline.bin {FACTORY_COPIES[0]}"
    )
    # `applied.sig` is written from the composed file, by the camera, with the
    # same byte range `S98_StitchCal` reads -- if the interactive path and the
    # boot path can disagree about what is installed, eventually they will.
    # `tail -c +9` rather than `dd bs=8 skip=1`: camsh refuses `dd` outright.
    out = sh(
        f"cd {CAM_DIR} && {stage_baseline} && "
        f"{helper} compose baseline.bin anchors.txt mesh_apply.bin "
        f"--require-baseline 2>&1 && "
        f"{helper} set 0 mesh_apply.bin --i-have-a-recovery-path 2>&1 && "
        f"tail -c +{MESH_SIG_SKIP + 1} mesh_apply.bin | md5sum | "
        f"cut -d' ' -f1 > applied.sig",
        host=host,
        timeout=120,
    )
    report["stages"].append(
        {
            "surface": "camera_mesh",
            "state": "applied" if "read-back matches" in out else "failed",
            "baseline_crc32": f"{baseline_crc:08x}",
            "vpe_id": 0,
            "shell": out.strip().splitlines()[-4:],
        }
    )
    if "read-back matches" not in out:
        raise RuntimeError(f"mesh write not confirmed:\n{out}")
    if f"{stats.result_crc32:08x}" not in out:
        raise RuntimeError(
            f"the camera composed a different mesh than this host did "
            f"(expected crc {stats.result_crc32:08x}):\n{out}"
        )

    if install_boot_config:
        # anchors.txt is already in place; that IS the boot configuration.
        report["boot_config"] = f"{CAM_DIR}/anchors.txt"
    report["stages"].append(
        {"surface": "downstream", "state": "disabled", "reason": "owned by camera_mesh"}
    )
    return report


def push_text(host: str, remote_path: str, text: str) -> None:
    """Write a small text file to the camera without base64 (there is none).

    Line by line through the probe shell, single-quoted. Only used for the
    few-hundred-byte anchors file; anything binary goes over `wget`.
    """
    if "'" in text:
        raise ValueError("single quotes cannot be pushed through this transport")
    cmds = [f"mkdir -p $(dirname {remote_path})", f": > {remote_path}"]
    cmds += [f"printf '%s\\n' '{line}' >> {remote_path}" for line in text.splitlines()]
    sh("\n".join(cmds), host=host)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "usage: stitch_apply.py <host> <anchors.txt> <password> [--dry-run]\n"
            "\n"
            "Applies the anchors to the camera in the only correct order:\n"
            "vendor scalars first, then a mesh composed onto the baseline they\n"
            "produced. Refuses if the baseline moves in between."
        )
        return 2
    host, anchors_path = argv[1], argv[2]
    password = argv[3] if len(argv) > 3 else ""
    dry = "--dry-run" in argv
    from lut2d import parse_anchors

    anchors, meta = parse_anchors(Path(anchors_path).read_text())
    try:
        report = apply_calibration(
            anchors,
            host=host,
            password=password,
            calibration_id=meta["calibration_id"],
            dry_run=dry,
        )
    except (OrderingViolation, Lut2DError) as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
