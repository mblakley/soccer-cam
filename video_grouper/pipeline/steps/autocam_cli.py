"""``autocam_cli`` step — drive Once AutoCam through its command-line front-end.

The sibling of the ``autocam`` step, with the desktop removed. Where ``autocam``
drives the vendor's *window* (launch, find windows, walk file dialogs, click
"Auto mark", poll a status label for magic substrings) and therefore must run in
the tray's interactive session, this step spawns ``AutocamCLI.exe`` and reads its
stdout. One process, one exit code, no window station — so it declares
``runtime = "service"`` and the Windows service runs it directly.

That difference is the whole point: every GUI-path failure observed in
production has been a *window* failure (a renamed process image, a dialog that
never appeared, a settings window whose UI thread died mid-render taking the
main window with it). None of those failure modes exists here.

Contract this step relies on (measured, not assumed — see
``docs/AUTOCAM_CLI_STEP.md``):

* ``--mode basic --input <path> --output <path>`` is the minimal invocation;
  every other flag is optional and defaults to the vendor's behaviour.
* Exit ``0`` = success, ``1`` = processing failure (reason on stdout), ``2`` =
  argument error.
* A machine-parseable ``status=...`` line lands on stdout roughly once a second
  carrying processed/total frame counts, elapsed, ms-per-frame and ETA, and the
  run ends on a terminal ``status=Succeeded`` / ``status=Failed`` line.
* Field marking is automatic and headless.

**Known behavioural gap:** the GUI path wrote a ``<output>.mp4.jsonl`` ball
sidecar that :func:`video_grouper.inference.phase_detector.ball_restarts` reads.
The CLI writes no such file, so a pipeline that uses this step loses the
ball-restart signal in phase detection (the player-on-field curve and whistle
signals remain — phase detection degrades, it does not break). This step logs
that explicitly on success rather than letting it disappear silently. See
``docs/AUTOCAM_CLI_STEP.md`` for the decision and the follow-up experiment.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from collections.abc import Iterable
from typing import Any, cast

from pydantic import BaseModel, field_validator

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest
from video_grouper.utils.mp4 import validate_video_output

logger = logging.getLogger(__name__)

# Inference providers, verbatim from the binary's own `help` output
# ("auto, cpu, cuda, dml, coreml"). Whitelisted so a typo fails at
# step-construction time with a readable message instead of costing an
# argument-error exit two minutes into a job.
_EXECUTION_PROVIDERS = ("auto", "cpu", "cuda", "dml", "coreml")

# `--mode` accepts `basic` or `stitch`. This step builds ONLY the basic-mode
# argv: stitch mode takes `--input-left`/`--input-right`/`--stitch-maps`
# instead of `--input`, a different command shape entirely. Accepting
# `mode = stitch` here would build a command that cannot run, so it is refused
# at config time rather than at the CLI's argument parser.
_SUPPORTED_MODES = ("basic",)

# Lines the CLI emits for its own telemetry. They contain the substring
# "RuntimeError" in the event name, so a naive "last line mentioning error"
# scan picks the telemetry URL instead of the real cause. Excluded from the
# failure-reason search.
_TELEMETRY_PREFIX = "[logger]"

# Prefix the CLI puts on the one-line human-readable failure cause.
_ERROR_PREFIX = "[cli] Error:"

# Progress lines arrive ~1/s; a 90-minute game is ~5400 of them. Log the first,
# then at most one per interval, then the terminal line.
_PROGRESS_LOG_INTERVAL_S = 60.0

# `key=value` tokens inside a progress line. Values are whitespace-free in
# every observed line; tokens that don't match are ignored rather than fatal.
_PROGRESS_TOKEN_RE = re.compile(r"(\w+)=(\S+)")

# How many trailing non-progress stdout lines to keep for the failure message.
_TAIL_LINES = 20


class AutocamCliStepConfig(BaseModel):
    """``[PIPELINE.<id>]`` config for the ``autocam_cli`` step.

    Only ``executable`` is required. Every other field is ``None``/empty by
    default and is simply *not passed*, so the default invocation is exactly the
    minimal command proven end-to-end — no flag we haven't measured is on the
    default path.
    """

    # Absolute path to AutocamCLI.exe (or a name resolvable on PATH).
    executable: str | None = None

    # Processing mode. "basic" is the proven value; exposed so an operator can
    # select another documented mode without needing extra_args (which cannot
    # override a flag this step already passes).
    mode: str = "basic"

    # Optional logo overlay burned into the output — the CLI's documented
    # "Add Logo" branding option. Point it at the team's own logo (or a
    # transparent PNG). Unset by default: no overlay configuration is applied.
    overlay_icon: str | None = None
    overlay_scale: float | None = None

    # Inference provider: auto | cpu | cuda | dml. Unset = the vendor default.
    execution_provider: str | None = None

    # Passed through verbatim; this step deliberately does not interpret or
    # validate the vendor's accepted spelling (e.g. "8M" vs "8000000",
    # "1920x1080"). A bad value surfaces as the CLI's argument-error exit.
    video_bitrate: str | None = None
    output_resolution: str | None = None

    # Escape hatch for any documented flag this model doesn't model. Appended
    # last so it can add (not replace) options. Accepts a comma-separated
    # string or a Python-literal list from INI, matching NodeConfig's
    # convention. Use the literal-list form for any value that itself contains
    # a comma, e.g. ``['--field-polygon', '10,20;30,40']``.
    extra_args: list[str] = []

    # Stall guard: fail the step if stdout produces nothing at all for this
    # long. NOT a total-runtime budget — a full game legitimately runs for over
    # an hour, and the fixed-deadline timeouts on the GUI path are exactly what
    # killed healthy renders. Startup includes a one-time engine-selection
    # benchmark before the first progress line, so keep this generous.
    progress_timeout_seconds: int = 900

    @field_validator("extra_args", mode="before")
    @classmethod
    def _parse_str_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            stripped = v.strip()
            if stripped in ("", "[]"):
                return []
            if stripped.startswith("[") and stripped.endswith("]"):
                import ast

                try:
                    parsed = ast.literal_eval(stripped)
                    if isinstance(parsed, list):
                        return [str(item) for item in parsed]
                except (ValueError, SyntaxError):
                    pass
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v

    @field_validator("execution_provider", mode="before")
    @classmethod
    def _check_provider(cls, v: Any) -> Any:
        if v is None:
            return None
        text = str(v).strip().lower()
        if not text:
            return None
        if text not in _EXECUTION_PROVIDERS:
            raise ValueError(
                f"execution_provider must be one of "
                f"{', '.join(_EXECUTION_PROVIDERS)} (got {v!r})"
            )
        return text

    @field_validator("mode", mode="before")
    @classmethod
    def _require_mode(cls, v: Any) -> Any:
        text = str(v or "").strip().lower()
        if text not in _SUPPORTED_MODES:
            raise ValueError(
                f"mode must be one of {', '.join(_SUPPORTED_MODES)} (got {v!r}). "
                "stitch mode needs --input-left/--input-right/--stitch-maps, "
                "which this step does not build."
            )
        return text

    @field_validator("overlay_scale")
    @classmethod
    def _check_overlay_scale(cls, v: float | None) -> float | None:
        # The CLI documents this as "from 0 to 1, relative to the output video
        # width"; catch an out-of-range value here rather than after a spawn.
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"overlay_scale must be between 0 and 1 (got {v})")
        return v


def build_command(
    config: AutocamCliStepConfig, input_path: str, output_path: str
) -> list[str]:
    """Build the argv for one render.

    Optional flags are appended only when configured, so the default argv is the
    minimal proven command. ``extra_args`` goes last.
    """
    argv: list[str] = [
        cast(str, config.executable),
        "--mode",
        config.mode,
        "--input",
        input_path,
        "--output",
        output_path,
    ]
    if config.execution_provider:
        argv += ["--execution-provider", config.execution_provider]
    if config.video_bitrate:
        argv += ["--video-bitrate", str(config.video_bitrate)]
    if config.output_resolution:
        argv += ["--output-resolution", str(config.output_resolution)]
    if config.overlay_icon:
        argv += ["--overlay-icon", config.overlay_icon]
    if config.overlay_scale is not None:
        argv += ["--overlay-scale", str(config.overlay_scale)]
    argv += list(config.extra_args)
    return argv


def parse_progress(line: str) -> dict[str, str] | None:
    """Parse one ``status=...`` progress line into its ``key=value`` tokens.

    Returns ``None`` for any line that isn't a progress line, so the caller can
    treat everything else as diagnostic output.
    """
    if "status=" not in line:
        return None
    tokens = dict(_PROGRESS_TOKEN_RE.findall(line))
    return tokens or None


def _format_progress(tokens: dict[str, str]) -> str:
    """Render a progress line for the log.

    Deliberately **not** an allowlist of known keys. An observed line is::

        status=Running processed=522 dropped=0 total=547 elapsed=00:01:04 \
msPerFrame=117.25 eta=00:00:02

    but a failing run emits ``total=N/A`` and ``eta=N/A``, and the GUI renders
    the same record as frames-per-second rather than ms-per-frame. Any
    allowlist written against one shape silently drops fields from another, so
    every token that isn't already rendered is passed through verbatim in the
    order received, and the percentage is computed only when ``total`` is a
    usable number.
    """
    processed = tokens.get("processed")
    total = tokens.get("total")
    parts = [f"status={tokens.get('status', '?')}"]
    if processed and total:
        try:
            pct = 100.0 * int(processed) / int(total)
            parts.append(f"{processed}/{total} frames ({pct:.1f}%)")
        except (TypeError, ValueError, ZeroDivisionError):
            parts.append(f"{processed}/{total} frames")
    elif processed:
        # No total reported — show the raw count rather than nothing.
        parts.append(f"{processed} frames")
    rendered = {"status", "processed", "total"}
    parts += [f"{k}={v}" for k, v in tokens.items() if k not in rendered and v]
    return " ".join(parts)


def _failure_reason(tail: Iterable[str]) -> str:
    """Pick the most useful line from the captured stdout tail.

    Order matters. A failing run ends with telemetry lines, one of which reads
    ``[logger] sending Autocam.Core.RuntimeError to https://...`` — it contains
    "Error", so a plain "last line mentioning error" scan reports a telemetry
    URL as the cause and buries the real one::

        [cli] Error: Could not open input 'X'. It may be unavailable, in use, \
or in an unsupported format.
        status=Failed processed=0 dropped=0 total=N/A ...
        [cli] status: Failed
        [logger] sending Autocam.Core.RuntimeError to https://...

    So: the explicit ``[cli] Error:`` line wins; otherwise any non-telemetry
    line mentioning an error; otherwise the last non-telemetry line.
    """
    lines = list(tail)
    for line in reversed(lines):
        if line.startswith(_ERROR_PREFIX):
            return line
    useful = [ln for ln in lines if not ln.startswith(_TELEMETRY_PREFIX)]
    for line in reversed(useful):
        if "error" in line.lower():
            return line
    if useful:
        return useful[-1]
    return lines[-1] if lines else "(no diagnostic output)"


def resolve_executable(executable: str | None) -> str | None:
    """Return a usable path for *executable*, or ``None`` if it can't be found."""
    if not executable:
        return None
    if os.path.isfile(executable):
        return executable
    return shutil.which(executable)


async def _spawn(argv: list[str]) -> asyncio.subprocess.Process:
    """Start the CLI with stderr folded into stdout.

    Indirection point: tests patch this instead of ``create_subprocess_exec`` so
    they never touch a real process.
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill of *proc* and any children it spawned."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            return
        except Exception as e:  # noqa: BLE001 - fall through to proc.kill()
            logger.debug("autocam_cli: taskkill on pid %s failed: %s", proc.pid, e)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


def _discard_invalid_output(output_path: str) -> None:
    """Delete *output_path* when it exists but isn't a complete video.

    A crashed or killed render leaves a partial MP4 behind; leaving it on disk
    invites a later resume into treating it as real work product.
    """
    if not os.path.isfile(output_path):
        return
    ok, reason = validate_video_output(output_path)
    if ok:
        logger.warning(
            "autocam_cli: run failed but %s validates as complete (%s); keeping it",
            output_path,
            reason,
        )
        return
    try:
        os.remove(output_path)
        logger.info("autocam_cli: removed partial output %s (%s)", output_path, reason)
    except OSError as e:
        logger.warning("autocam_cli: could not remove partial %s: %s", output_path, e)


class AutocamCliStep(PipelineStep[AutocamCliStepConfig]):
    name = "autocam_cli"
    config_model = AutocamCliStepConfig
    consumes = ("input_path",)
    produces = ("output_path",)
    # Session-0 safe: no desktop, no window station, no user profile needed.
    runtime = "service"
    # Pure stdlib subprocess — available in every bundle, including the tray's.
    requires = ()
    # The engine runs inference on a GPU provider; serialize with the other
    # GPU steps rather than on the (window-scoped, now irrelevant) autocam_ui tag.
    resources = ("gpu",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        del ctx  # this step needs nothing from the run context
        input_path = cast(str, manifest.get("input_path"))
        output_path = cast(str, manifest.get("output_path"))

        exe = resolve_executable(self.config.executable)
        if not exe:
            logger.error(
                "autocam_cli: executable %r not found — set `executable` in the "
                "step's config to the full path of AutocamCLI.exe",
                self.config.executable,
            )
            return False
        if not os.path.isfile(input_path):
            logger.error("autocam_cli: input %s does not exist", input_path)
            return False

        cfg = self.config.model_copy(update={"executable": exe})
        argv = build_command(cfg, input_path, output_path)
        logger.info("autocam_cli: running %s", " ".join(argv))

        try:
            proc = await _spawn(argv)
        except (OSError, ValueError) as e:
            logger.error("autocam_cli: could not start %s: %s", exe, e)
            return False

        tail: deque[str] = deque(maxlen=_TAIL_LINES)
        last_status: str | None = None
        last_log = 0.0
        started = time.monotonic()

        stdout = proc.stdout
        if stdout is None:  # pragma: no cover - _spawn always pipes stdout
            logger.error("autocam_cli: child process has no stdout pipe")
            _kill_tree(proc)
            return False

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        stdout.readline(),
                        timeout=self.config.progress_timeout_seconds,
                    )
                except TimeoutError:
                    logger.error(
                        "autocam_cli: no output for %ss (last status=%s); killing pid %s",
                        self.config.progress_timeout_seconds,
                        last_status or "none",
                        proc.pid,
                    )
                    _kill_tree(proc)
                    # Bounded: if the kill somehow didn't take, don't trade a
                    # stalled render for a stalled pipeline.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=30)
                    _discard_invalid_output(output_path)
                    return False

                if not raw:  # EOF — the process is done writing
                    break

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                tokens = parse_progress(line)
                if tokens is None:
                    tail.append(line)
                    logger.debug("autocam_cli: %s", line)
                    continue

                last_status = tokens.get("status")
                now = time.monotonic()
                if now - last_log >= _PROGRESS_LOG_INTERVAL_S or last_log == 0.0:
                    last_log = now
                    logger.info("autocam_cli: %s", _format_progress(tokens))

            rc = await proc.wait()
        except asyncio.CancelledError:
            # Service shutdown / task cancellation: never orphan a render that
            # would keep a GPU and an output file busy for another hour. Every
            # statement here is synchronous on purpose — awaiting inside a
            # cancelled task can re-raise before the cleanup finishes.
            logger.warning("autocam_cli: cancelled; killing pid %s", proc.pid)
            _kill_tree(proc)
            _discard_invalid_output(output_path)
            raise

        elapsed = time.monotonic() - started
        if rc != 0:
            reason = _failure_reason(tail)
            logger.error(
                "autocam_cli: exit %s after %.0fs (last status=%s): %s",
                rc,
                elapsed,
                last_status or "none",
                reason,
            )
            _discard_invalid_output(output_path)
            return False

        ok, reason = validate_video_output(output_path, input_path)
        if not ok:
            logger.error(
                "autocam_cli: exit 0 but the output is not a complete video: %s",
                reason,
            )
            _discard_invalid_output(output_path)
            return False

        manifest.put("output_path", output_path)
        logger.info(
            "autocam_cli: complete in %.0fs — %s (last status=%s)",
            elapsed,
            reason,
            last_status or "none",
        )
        # Surfaced every run so the gap can't be forgotten: unlike the GUI path,
        # the CLI writes no per-frame ball sidecar, so phase detection runs
        # without its ball-restart signal. See docs/AUTOCAM_CLI_STEP.md.
        logger.info(
            "autocam_cli: no ball-position sidecar is produced by the CLI; "
            "phase detection runs on the player-curve + whistle signals only"
        )
        return True


register_step(AutocamCliStep.name, AutocamCliStep, AutocamCliStepConfig)
