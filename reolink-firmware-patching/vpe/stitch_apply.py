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
    3. dump the NEW factory mesh -- this is the baseline
    4. compose the correction onto THAT
    5. write, with read-back verification
    6. re-dump and confirm the baseline did not move under us

Step 6 is the guard that makes step 1 unskippable in practice: if anything
re-ran the optimiser between the baseline dump and the write -- another
operator, the app, a second copy of this tool -- the composed mesh no longer
matches its baseline and the write is refused rather than silently applied on
top of a different calibration.

The boot hook (`S98_StitchCal`) satisfies the same constraint structurally
instead: it composes onto whatever mesh the firmware generated at boot, which by
construction already reflects the persisted scalars.

Transport: HTTP for the scalars and the snapshot, the port-2323 probe shell for
everything else, `wget` for pushing files (there is no base64 on the device).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

from camsh import sh  # noqa: E402
from lut2d import (  # noqa: E402
    Lut2D,
    Lut2DError,
    compose_from_anchors_file,
    crc32,
    format_anchors,
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

    # 2+3) settle, then dump THIS baseline
    baseline = wait_for_stable_mesh(host)
    baseline_crc = crc32(baseline)
    report["baseline_crc32"] = f"{baseline_crc:08x}"

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

    # 6) re-check the baseline before writing. This is the ordering guard: if
    #    anything re-ran the optimiser since step 3, the mesh we hold was
    #    composed against a calibration that no longer exists.
    dump_mesh(host, name="recheck.bin")
    if crc32(read_mesh(host, "recheck.bin")) != baseline_crc:
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
    out = sh(
        f"cd {CAM_DIR} && "
        f"{_helper(host)} compose baseline.bin anchors.txt mesh_apply.bin "
        f"--require-baseline 2>&1 && "
        f"{_helper(host)} set 0 mesh_apply.bin --i-have-a-recovery-path 2>&1",
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
