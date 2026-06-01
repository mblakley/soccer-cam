"""Config-driven pipeline queue processor.

Drains the PIPELINE queue, runs each game through its configured
:class:`~video_grouper.pipeline.runner.PipelineRunner`, and on a *complete* run
flips the group's ``state.json`` to ``pipeline_complete`` and (when YouTube is
enabled) hands off to the upload processor.

It runs in one of two runtimes:

* ``service`` — Session-0-safe compute steps (stitch_correct, detect, track,
  render). A tray-runtime step (e.g. autocam) encountered here returns
  ``awaiting`` and the group is left for the tray to pick up.
* ``tray``    — interactive-desktop steps (autocam). A service-runtime step
  encountered here returns ``awaiting`` and is left for the service.

Cross-runtime handoff is mediated entirely by the per-group manifest, so the
two runtimes resume each other without direct coordination.

Concurrency: this drains one game at a time
(``_semaphore`` capacity 1). Cross-game parallelism is future work; the shared
:class:`~video_grouper.pipeline.resources.ResourceManager` is the seam for it —
it already serializes GPU/UI-bound steps so several PipelineProcessors (or a
future multi-game runner) can't oversubscribe a scarce resource.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
from pathlib import Path

from .base_queue_processor import QueueProcessor
from .queue_type import QueueType
from .tasks.pipeline import PipelineTask
from video_grouper.utils.config import Config

logger = logging.getLogger(__name__)


class PipelineProcessor(QueueProcessor):
    """Processes pipeline tasks sequentially (one game at a time per runtime)."""

    def __init__(
        self,
        storage_path: str,
        config: Config,
        upload_processor=None,
        runtime: str = "service",
        resource_manager=None,
    ):
        super().__init__(storage_path, config)
        self.upload_processor = upload_processor
        self.runtime = runtime
        # One ResourceManager shared by every run this processor drives. Built
        # from config when not supplied so a standalone processor still gates
        # GPU/RAM-heavy/UI steps correctly.
        if resource_manager is None:
            from video_grouper.pipeline.resources import build_resource_manager

            resource_manager = build_resource_manager(
                gpu_concurrency=config.pipeline.gpu_concurrency,
                ram_heavy_concurrency=config.pipeline.ram_heavy_concurrency,
            )
        self.resource_manager = resource_manager
        # Only one game runs at a time within this processor (cross-game
        # parallelism is future work; the ResourceManager is the seam for it).
        self._semaphore = asyncio.Semaphore(1)

    @property
    def queue_type(self) -> QueueType:
        return QueueType.PIPELINE

    def get_item_key(self, item: PipelineTask) -> str:
        return f"{item.task_type}:{item.get_item_path()}:{hash(item)}"

    async def process_item(self, item: PipelineTask) -> None:
        async with self._semaphore:
            await self._process_one(item)

    async def _process_one(self, item: PipelineTask) -> None:
        try:
            # Pre-flight: skip a group that's already finished the pipeline (a
            # stale task restored from disk after a crash). Re-running would
            # cost hours of GPU time on output we already have. We accept BOTH
            # the new and legacy completion statuses so a mixed-history install
            # never re-runs a completed game.
            state_file = item.group_dir / "state.json"
            if state_file.exists():
                try:
                    with open(state_file, "r") as f:
                        current_status = json.load(f).get("status")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(
                        "PIPELINE: could not read state.json for %s "
                        "(continuing with task): %s",
                        item.group_dir.name,
                        e,
                    )
                    current_status = None
                if current_status in (
                    "pipeline_complete",
                    "ball_tracking_complete",
                    "complete",
                ):
                    logger.info(
                        "PIPELINE: skipping %s — group already at status '%s' "
                        "(likely a stale task restored from disk).",
                        item.group_dir.name,
                        current_status,
                    )
                    return

            logger.info(
                "PIPELINE: processing task (%s runtime): %s", self.runtime, item
            )

            from video_grouper.pipeline.base import StepContext
            from video_grouper.pipeline.runner import PipelineRunner

            team_name = item.team_name
            ttt_dump = item.ttt_config
            if ttt_dump is None:
                # Discovery may not have stamped it (e.g. recovery path); fall
                # back to the live config the same way the legacy path did.
                ttt_dump = (
                    self.config.ttt.model_dump()
                    if getattr(self.config, "ttt", None) and self.config.ttt.enabled
                    else None
                )

            ctx = StepContext(
                group_dir=item.group_dir,
                team_name=team_name,
                storage_path=Path(item.storage_path or self.storage_path),
                ttt_config=ttt_dump,
            )
            runner = PipelineRunner(
                self.config.pipeline.ordered_steps(team_name),
                runtime=self.runtime,
                resource_manager=self.resource_manager,
            )
            result = await runner.run(item.input_path, item.output_path, ctx)

            if result.status == "complete":
                logger.info("PIPELINE: task completed: %s", item)
                await self._handle_successful_completion(item)
            elif result.status == "awaiting":
                logger.info(
                    "PIPELINE: %s awaiting runtime %r; leaving for the other "
                    "runtime to resume.",
                    item.group_dir.name,
                    result.awaiting_runtime,
                )
            else:  # failed
                logger.error(
                    "PIPELINE: task failed at step %s for %s: %s",
                    result.failed_step,
                    item.group_dir.name,
                    result.error,
                )
                await self._mark_failed(item, result.error)
        except Exception as e:
            logger.error("PIPELINE: error processing task %s: %s", item, e)

    @staticmethod
    def _read_team_name(group_dir: Path) -> str | None:
        """Read team_name from match_info.ini if present, else None.

        Mirrors PipelineDiscoveryProcessor._read_team_name so the processor
        can recover a team name when a task arrives without one.
        """
        match_info_path = group_dir / "match_info.ini"
        if not match_info_path.exists():
            return None
        try:
            parser = configparser.ConfigParser()
            parser.read(match_info_path, encoding="utf-8")
            if parser.has_option("MATCH", "team_name"):
                return parser.get("MATCH", "team_name") or None
        except Exception as e:
            logger.debug(
                "PIPELINE: could not read team_name from %s: %s", match_info_path, e
            )
        return None

    async def _handle_successful_completion(self, item: PipelineTask) -> None:
        group_dir = item.group_dir
        group_name = group_dir.name

        logger.info(
            "PIPELINE: updating group '%s' status to pipeline_complete", group_name
        )
        state_file = group_dir / "state.json"

        if not state_file.exists():
            logger.warning(
                "PIPELINE: state.json not found for group %s on success", group_name
            )
            return

        try:
            with open(state_file, "r") as f:
                state_data = json.load(f)
            state_data["status"] = "pipeline_complete"
            with open(state_file, "w") as f:
                json.dump(state_data, f, indent=4)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                "PIPELINE: could not update status for group %s: %s", group_name, e
            )
            return

        if self.config.youtube.enabled:
            logger.info("PIPELINE: queuing YouTube upload for group %s", group_name)
            await self._add_to_youtube_queue(group_dir)
        else:
            logger.info(
                "PIPELINE: YouTube uploads disabled; skipping upload for %s", group_name
            )

    async def _mark_failed(self, item: PipelineTask, error: str | None) -> None:
        """Record a pipeline failure on the group's state.json (non-fatal).

        Sets ``status = pipeline_failed`` so the dashboard / auditor can surface
        the stuck game; the per-step manifest holds the detailed error.
        """
        state_file = item.group_dir / "state.json"
        if not state_file.exists():
            return
        try:
            with open(state_file, "r") as f:
                state_data = json.load(f)
            state_data["status"] = "pipeline_failed"
            if error:
                state_data["pipeline_error"] = error
            with open(state_file, "w") as f:
                json.dump(state_data, f, indent=4)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                "PIPELINE: could not record failure for group %s: %s",
                item.group_dir.name,
                e,
            )

    async def _add_to_youtube_queue(self, group_dir: Path) -> None:
        try:
            from video_grouper.task_processors.tasks.upload import YoutubeUploadTask

            relative_group_dir = group_dir.relative_to(Path(self.storage_path))
            youtube_task = YoutubeUploadTask(group_dir=str(relative_group_dir))

            if self.upload_processor:
                await self.upload_processor.add_work(youtube_task)
                logger.info(
                    "PIPELINE: queued YouTube upload for group %s", group_dir.name
                )
            else:
                logger.warning(
                    "PIPELINE: no upload processor available for group %s",
                    group_dir.name,
                )
        except Exception as e:
            logger.error(
                "PIPELINE: error creating YouTube upload task for %s: %s",
                group_dir.name,
                e,
            )
