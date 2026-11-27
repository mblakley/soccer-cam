"""Run the E2E suite and report the outcome somewhere a human will see it.

Built for a weekly scheduled run. The E2E suite is the only thing that
exercises the whole chain -- camera discovery, download, grouping, combine,
trim, AutoCam, upload -- and nothing else notices when a link breaks: CI runs
no tests at all, and a broken link is silent until someone records a game.

Reports over NTFY because that is where the camera manager already looks.

Two failure classes are called out by name rather than lumped into "the tests
failed", because they mean completely different things:

* **AutoCam needs attention.** The vendor app refuses to process -- an update
  prompt, an expired licence. Nothing in soccer-cam is wrong and no game will
  process until a human opens AutoCam. This is the case that prompted the
  script: it had been failing silently as a generic timeout.
* **The environment is not ready.** Docker down, no clips staged. The run
  never got far enough to say anything about the pipeline, so reporting it as
  a pipeline failure would be a lie.

Usage::

    uv run python -m scripts.run_weekly_e2e            # run and report
    uv run python -m scripts.run_weekly_e2e --dry-run  # check prerequisites only
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
E2E_TEST = "tests/e2e/test_runner.py::TestE2EPipeline::test_complete_pipeline"

# The harness caps its own progress monitor at 10 minutes, but AutoCam renders
# 7680x2160 footage at ~6 fps, so a two-game run legitimately takes a while.
# Generous enough not to cut off real work; bounded so a wedged run does not
# still hold the machine when next week's fires.
RUN_TIMEOUT_SECONDS = 60 * 45
CLIPS_DIR = PROJECT_ROOT / "tests/e2e/test_clips"

# Where AutoCam's own words end up: the tray drives the GUI and logs what it
# reads off the status panel.
#
# ONLY per-run logs may be consulted. The harness opens this one with mode "w",
# so it holds this run and nothing else. The application's own log
# (test_data/.../video_grouper_e2e_test.log) is a TimedRotatingFileHandler and
# APPENDS across runs -- reading it made every later failure inherit an earlier
# run's AutoCam message and get reported as "AutoCam needs attention" when the
# real cause was something else entirely.
TRAY_LOG = PROJECT_ROOT / "tests/e2e/test_logs/tray_subprocess.log"

# Vendor text meaning "a human must open AutoCam". Mirrors
# autocam_automation._NEEDS_ATTENTION_MARKERS; kept as its own copy so this
# script never imports the tray driver (which loads pywinauto at import).
AUTOCAM_NEEDS_ATTENTION = re.compile(
    r"please update autocam|contact support|licen[cs]e has expired|invalid licen[cs]e",
    re.IGNORECASE,
)


@dataclass
class Outcome:
    ok: bool
    title: str
    detail: str
    priority: int = 3


def check_prerequisites() -> Outcome | None:
    """Return an Outcome when the run cannot meaningfully start."""
    if shutil.which("docker") is None:
        return Outcome(
            False,
            "Weekly E2E skipped: no Docker",
            "The camera simulator needs Docker; it is not on PATH.",
        )
    probe = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=60
    )
    if probe.returncode != 0:
        return Outcome(
            False,
            "Weekly E2E skipped: Docker not running",
            "The camera simulator needs Docker; the daemon did not respond.",
        )
    if not sorted(CLIPS_DIR.glob("*.mp4")):
        return Outcome(
            False,
            "Weekly E2E skipped: no clips staged",
            f"{CLIPS_DIR} is empty. Run "
            "`uv run python -m tests.e2e.make_test_clips` to stage recordings.",
        )
    return None


def reset_simulator() -> None:
    """Drop the simulator AND its volume.

    Without ``-v`` the simulator keeps serving whatever clips it seeded first,
    so a restaged fixture silently has no effect -- which invalidated a run
    here before anyone noticed.
    """
    subprocess.run(
        ["docker", "compose", "--profile", "reolink", "down", "-v"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=180,
    )


def classify_failure(pytest_output: str) -> Outcome:
    """Turn a failed run into something worth waking up to."""
    haystacks = [pytest_output]
    try:
        haystacks.append(TRAY_LOG.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    combined = "\n".join(haystacks)

    match = AUTOCAM_NEEDS_ATTENTION.search(combined)
    if match:
        # Quote AutoCam rather than paraphrasing: the exact words are what
        # tells you whether to update, renew, or call the vendor.
        line = next(
            (
                ln.strip()
                for ln in combined.splitlines()
                if AUTOCAM_NEEDS_ATTENTION.search(ln)
            ),
            match.group(0),
        )
        return Outcome(
            False,
            "AutoCam needs attention",
            f"The weekly E2E run could not process video. AutoCam says:\n\n"
            f"{line}\n\n"
            f"No game will render until AutoCam is opened and dealt with.",
            priority=4,
        )

    tail = "\n".join(pytest_output.strip().splitlines()[-15:])
    return Outcome(
        False,
        "Weekly E2E failed",
        f"The end-to-end pipeline test did not pass.\n\n{tail}",
        priority=4,
    )


def notify(outcome: Outcome) -> None:
    """Publish to the configured NTFY topic. Never raises."""
    try:
        import httpx

        from video_grouper.utils.config import load_config

        config = load_config(PROJECT_ROOT / "shared_data" / "config.ini")
        ntfy = config.ntfy
        if not ntfy.enabled or not ntfy.topic:
            print("NTFY not configured; outcome not published.")
            return
        httpx.post(
            f"{ntfy.server_url.rstrip('/')}/{ntfy.topic}",
            content=outcome.detail.encode("utf-8"),
            headers={
                "Title": outcome.title,
                "Priority": str(outcome.priority),
                "Tags": "white_check_mark" if outcome.ok else "rotating_light",
            },
            timeout=30,
        )
        print(f"Published to ntfy topic {ntfy.topic!r}.")
    except Exception as exc:  # noqa: BLE001 — reporting must never mask the result
        print(f"Could not publish outcome over NTFY: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check prerequisites and exit without running the suite",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="print the outcome instead of sending it",
    )
    args = parser.parse_args()

    blocked = check_prerequisites()
    if blocked is not None:
        print(f"{blocked.title}: {blocked.detail}")
        if not args.no_notify:
            notify(blocked)
        return 1

    if args.dry_run:
        print("Prerequisites OK: Docker is up and clips are staged.")
        return 0

    reset_simulator()
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", E2E_TEST, "-q", "-p", "no:randomly"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        # A scheduled job must report, never traceback. Raising here also threw
        # away everything pytest had written, so the one run that most needed
        # diagnosis produced none. Salvage whatever was captured before the
        # kill and classify on that -- an AutoCam refusal is still visible in
        # the tray log even when the suite never returned.
        partial = (
            (expired.stdout or b"").decode("utf-8", "replace")
            if isinstance(expired.stdout, bytes)
            else (expired.stdout or "")
        )
        print(partial[-4000:])
        outcome = classify_failure(partial)
        outcome.title = f"{outcome.title} (timed out)"
        outcome.detail = (
            f"The suite was still running after "
            f"{RUN_TIMEOUT_SECONDS // 60} minutes and was stopped.\n\n"
            f"{outcome.detail}"
        )
        print(f"\n{outcome.title}\n{outcome.detail}")
        if not args.no_notify:
            notify(outcome)
        return 1

    print(proc.stdout[-4000:])

    if proc.returncode == 0:
        outcome = Outcome(
            True,
            "Weekly E2E passed",
            "Camera discovery through upload completed end to end.",
            priority=2,
        )
    else:
        outcome = classify_failure(proc.stdout + "\n" + proc.stderr)

    print(f"\n{outcome.title}\n{outcome.detail}")
    if not args.no_notify:
        notify(outcome)
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
