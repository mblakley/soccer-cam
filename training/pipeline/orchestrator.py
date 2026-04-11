"""Pipeline orchestrator — populates work queues and monitors pipeline health.

Starts the API server in a background thread, then uses it as a client
for all DB operations. Only the API thread touches SQLite directly.

Usage:
    uv run python -m training.pipeline run
    uv run python -m training.pipeline run --once
    uv run python -m training.pipeline run --dry-run
"""

import json
import logging
import subprocess
import time
from pathlib import Path

from training.pipeline.client import PipelineClient
from training.pipeline.config import load_config
from training.pipeline.state_machine import (
    advance_state,
    is_failed,
    next_task_for_game,
)

logger = logging.getLogger(__name__)


class Orchestrator:
    """Queue-populating orchestrator. Uses HTTP API for all DB access."""

    def __init__(self, config_path: Path | None = None, dry_run: bool = False):
        self.cfg = load_config(config_path)
        self.dry_run = dry_run
        self.api = PipelineClient("http://127.0.0.1:8643")
        self._last_qa_time = 0.0
        self._qa_count_this_hour = 0
        self._alerted_workers: set[str] = set()

    def run(self, once: bool = False):
        """Main orchestrator loop. Requires API server to be running separately."""
        logger.info("Orchestrator starting (interval=%ds, dry_run=%s)",
                     self.cfg.orchestrator.check_interval, self.dry_run)

        # Wait for API to be available
        if not self.dry_run:
            while not self.api.is_available():
                logger.info("Waiting for API server at %s...", self.api.base_url)
                time.sleep(5)

        while True:
            try:
                self._tick()
            except Exception:
                logger.exception("Orchestrator tick failed")

            if once:
                break

            time.sleep(self.cfg.orchestrator.check_interval)

    def _tick(self):
        """One pass of the orchestrator loop."""
        self._collect_results()
        self.api.reclaim_stale(self.cfg.orchestrator.stale_heartbeat)
        self._enqueue_work()

    # ------------------------------------------------------------------
    # Collect results
    # ------------------------------------------------------------------

    def _collect_results(self):
        """Check completed work items and advance game states."""
        done_items = self.api.get_queue_items(status="done")
        for item in done_items:
            game_id = item.get("game_id")
            task_type = item["task_type"]

            if game_id:
                game = self.api.get_game(game_id)
                if game:
                    current_state = game.get("pipeline_state", "")
                    new_state = advance_state(current_state, task_type, success=True)
                    if new_state != current_state:
                        if not self.dry_run:
                            self.api.set_game_state(game_id, new_state)
                            self.api.reset_attempts(game_id)

                            result = item.get("result")
                            if isinstance(result, str):
                                try:
                                    result = json.loads(result)
                                except (json.JSONDecodeError, TypeError):
                                    result = {}
                            if result:
                                self._update_stats_from_result(game_id, task_type, result)

                        logger.info("Game %s: %s -> %s (via %s)",
                                     game_id, current_state, new_state, task_type)
                        self.api.log_event(
                            "info",
                            f"{game_id} {current_state} -> {new_state}",
                            category="state_change",
                            game_id=game_id,
                        )

            # Archive processed items so they don't get re-processed
            if not self.dry_run:
                self.api.archive(item["id"])

        # Check failed items
        failed = self.api.get_queue_items(status="failed")
        for item in failed:
            game_id = item.get("game_id")
            if game_id:
                game = self.api.get_game(game_id)
                if game:
                    current_state = game.get("pipeline_state", "")
                    if not is_failed(current_state):
                        new_state = f"FAILED:{current_state}"
                        if not self.dry_run:
                            self.api.set_game_state(game_id, new_state, error=item.get("error"))
                            self.api.increment_attempts(game_id)

                        # Only notify on final failure (max attempts reached)
                        game_info = self.api.get_game(game_id) if game_id else None
                        attempts = game_info.get("pipeline_attempts", 0) if game_info else 0
                        max_attempts = item.get("max_attempts", 3)
                        if attempts >= max_attempts:
                            self._ntfy(
                                f"Task failed (final): {item['task_type']} for {game_id}\n"
                                f"Error: {item.get('error', 'unknown')[:200]}",
                                title="Task Failed",
                                priority="default",
                            )

    def _update_stats_from_result(self, game_id: str, task_type: str, result: dict):
        if task_type == "tile":
            self.api.update_game_stats(
                game_id,
                tile_count=result.get("tiles"),
                segment_count=result.get("segments"),
            )
        elif task_type == "label":
            self.api.update_game_stats(
                game_id,
                label_count=result.get("labels_written"),
            )
        elif task_type == "train":
            metrics = result.get("metrics", {})
            if metrics:
                self._ntfy(
                    f"Training {result.get('version', '?')} complete!\n"
                    f"mAP50: {metrics.get('mAP50', 0):.3f}, "
                    f"P: {metrics.get('precision', 0):.3f}, "
                    f"R: {metrics.get('recall', 0):.3f}",
                    title="Training Complete",
                )

    # ------------------------------------------------------------------
    # Enqueue work
    # ------------------------------------------------------------------

    def _enqueue_work(self):
        games = self.api.get_games_needing_work()

        for game in games:
            game_id = game["game_id"]
            state = game["pipeline_state"]

            if game.get("pipeline_attempts", 0) >= 3:
                continue

            task_type = next_task_for_game(state)
            if not task_type:
                continue

            if self.api.has_active_item(task_type, game_id):
                continue

            if task_type == "stage":
                active_stages = self.api.get_queue_items(status="running")
                claimed_stages = self.api.get_queue_items(status="claimed")
                stage_count = sum(1 for i in active_stages + claimed_stages if i.get("task_type") == "stage")
                if stage_count >= self.cfg.orchestrator.max_staging_concurrent:
                    continue

            if task_type == "sonnet_qa" and not self._can_qa():
                continue

            payload = self._build_payload(game, task_type)
            priority = self._get_priority(task_type, game)
            target = self._get_target_machine(task_type)

            if self.dry_run:
                logger.info("[DRY RUN] Would enqueue %s for %s (priority=%d, target=%s)",
                             task_type, game_id, priority, target or "any")
            else:
                self.api.enqueue(
                    task_type,
                    game_id=game_id,
                    priority=priority,
                    target_machine=target,
                    payload=payload,
                )

        self._maybe_enqueue_training()

    def _build_payload(self, game: dict, task_type: str) -> dict:
        payload = {}
        if task_type == "stage":
            payload["video_path"] = game.get("video_path", "")
        if task_type in ("tile", "label"):
            payload["needs_flip"] = bool(game.get("needs_flip"))
            payload["camera_type"] = game.get("camera_type", "dahua")
        return payload

    def _get_priority(self, task_type: str, game: dict) -> int:
        base = {
            "ingest_reviews": 5, "train": 10, "label": 20,
            "measure_coverage": 25, "fill_gaps": 30, "tile": 35,
            "stage": 40, "sonnet_qa": 50, "generate_review": 55,
        }.get(task_type, 50)
        if game.get("label_count", 0) > 0:
            base -= 5
        return base

    def _get_target_machine(self, task_type: str) -> str | None:
        if task_type == "stage":
            return self.cfg.server.hostname
        if task_type == "train":
            for name, m in self.cfg.machines.items():
                if "train" in m.capabilities:
                    return m.hostname
        return None

    def _maybe_enqueue_training(self):
        if self.api.has_active_item("train"):
            return
        trainable = self.api.get_trainable_games()
        if len(trainable) < self.cfg.orchestrator.min_new_games_for_retrain:
            return
        train_games = [g["game_id"] for g in trainable]
        val_games = train_games[:1]
        train_games = train_games[1:]
        if not train_games:
            return

        version = f"v3.{int(time.time()) % 10000}"
        if self.dry_run:
            logger.info("[DRY RUN] Would enqueue training %s", version)
        else:
            self.api.enqueue(
                "train",
                priority=10,
                target_machine=self._get_target_machine("train"),
                payload={"train_games": train_games, "val_games": val_games, "version": version},
            )

    def _can_qa(self) -> bool:
        now = time.time()
        if now - self._last_qa_time > 3600:
            self._qa_count_this_hour = 0
        if self._qa_count_this_hour >= self.cfg.qa.sonnet_batch_limit:
            return False
        self._qa_count_this_hour += 1
        self._last_qa_time = now
        return True

    # ------------------------------------------------------------------
    # Worker health
    # ------------------------------------------------------------------

    def _check_workers(self):
        status = self.api.get_status()
        if not status:
            return
        now = time.time()
        for w in status.get("workers", []):
            hostname = w.get("hostname", "?")
            worker_status = w.get("status", "")
            age = now - (w.get("last_seen") or 0)

            # Don't alert for workers that yielded for games — that's expected
            if worker_status == "yielded":
                continue

            # Only alert once per worker (track in _alerted_workers set)
            if age > self.cfg.orchestrator.stale_heartbeat:
                if hostname not in self._alerted_workers:
                    self._alerted_workers.add(hostname)
                    self._ntfy(
                        f"Worker {hostname} hasn't reported in {age / 60:.0f} min",
                        title="Worker Down?",
                        priority="high",
                    )
            else:
                # Worker recovered — clear alert
                self._alerted_workers.discard(hostname)

            if (w.get("gpu_temp_c") or 0) > 90:
                self._ntfy(
                    f"Worker {hostname} GPU at {w['gpu_temp_c']}C!",
                    title="GPU Overheating",
                    priority="urgent",
                )

    # ------------------------------------------------------------------
    # NTFY
    # ------------------------------------------------------------------

    def _ntfy(self, message: str, title: str = "Pipeline", priority: str = "default"):
        if not self.cfg.ntfy.enabled or self.dry_run:
            logger.info("[NTFY %s] %s: %s", priority, title, message)
            return
        try:
            subprocess.run(
                ["curl", "-s", "-H", f"Title: {title}", "-H", f"Priority: {priority}",
                 "-d", message, f"https://ntfy.sh/{self.cfg.ntfy.topic}"],
                capture_output=True, timeout=10,
            )
        except Exception as e:
            logger.warning("NTFY failed: %s", e)
