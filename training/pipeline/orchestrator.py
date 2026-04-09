"""Pipeline orchestrator — populates work queues and monitors pipeline health.

Long-running process on the server that:
1. Scans game states in registry.db → enqueues work items
2. Monitors worker health via heartbeats
3. Reclaims stale items
4. Advances game states on task completion
5. Sends NTFY notifications for milestones and problems

The orchestrator does NOT execute tasks or push work to machines.
It just keeps the queue populated and watches for problems.
Workers pull their own work.

Usage:
    uv run python -m training.pipeline run
    uv run python -m training.pipeline run --once
    uv run python -m training.pipeline run --dry-run
"""

import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from training.pipeline.config import load_config
from training.pipeline.queue import WorkQueue
from training.pipeline.registry import GameRegistry
from training.pipeline.state_machine import (
    advance_state,
    is_failed,
    next_task_for_game,
)

logger = logging.getLogger(__name__)


class Orchestrator:
    """Queue-populating orchestrator."""

    def __init__(self, config_path: Path | None = None, dry_run: bool = False):
        self.cfg = load_config(config_path)
        self.dry_run = dry_run
        self.queue = WorkQueue(self.cfg.paths.work_queue_db)
        self.registry = GameRegistry(self.cfg.paths.registry_db)
        self._last_qa_time = 0.0
        self._qa_count_this_hour = 0

    def run(self, once: bool = False):
        """Main orchestrator loop."""
        # Start the HTTP API server in a background thread
        if not self.dry_run:
            from training.pipeline.api import start_api
            start_api(self.queue, self.registry, self.cfg, port=8643)

        logger.info("Orchestrator starting (interval=%ds, dry_run=%s)",
                     self.cfg.orchestrator.check_interval, self.dry_run)

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
        # P1: Collect completed results — advance game states
        self._collect_results()

        # P2: Reclaim stale items
        stale = self.queue.reclaim_stale(self.cfg.orchestrator.stale_heartbeat)
        for item in stale:
            self._ntfy(
                f"Stale task reclaimed: {item['task_type']} {item.get('game_id', '')} "
                f"(was on {item.get('claimed_by', '?')})",
                title="Worker Stale",
                priority="high",
            )

        # P3-P11: Enqueue work based on game states
        self._enqueue_work()

        # Monitor worker health
        self._check_workers()

    # ------------------------------------------------------------------
    # Collect results
    # ------------------------------------------------------------------

    def _collect_results(self):
        """Check completed work items and advance game states."""
        done_items = self.queue.get_items(status="done")
        for item in done_items:
            game_id = item.get("game_id")
            task_type = item["task_type"]

            if game_id:
                current_state = self.registry.get_state(game_id)
                if current_state:
                    new_state = advance_state(current_state, task_type, success=True)
                    if new_state != current_state:
                        if not self.dry_run:
                            self.registry.set_state(game_id, new_state)
                            self.registry.reset_attempts(game_id)

                            # Update stats from result
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

                        self.queue.log_event(
                            "info",
                            f"{game_id} {current_state} -> {new_state}",
                            category="state_change",
                            game_id=game_id,
                        )

            # Mark as acknowledged (move from done to avoid re-processing)
            # We do this by not re-querying done items — the queue keeps them
            # but we only process items we haven't seen.
            # For simplicity, update status to 'archived'
            if not self.dry_run:
                conn = self.queue._get_conn()
                conn.execute(
                    "UPDATE work_items SET status = 'archived' WHERE id = ? AND status = 'done'",
                    (item["id"],),
                )
                conn.commit()

        # Check failed items
        failed = self.queue.get_failed_items()
        for item in failed:
            game_id = item.get("game_id")
            if game_id:
                current_state = self.registry.get_state(game_id)
                if current_state and not is_failed(current_state):
                    new_state = f"FAILED:{current_state}"
                    if not self.dry_run:
                        self.registry.set_state(game_id, new_state, error=item.get("error"))
                        self.registry.increment_attempts(game_id)

                    self._ntfy(
                        f"Task failed: {item['task_type']} for {game_id}\n"
                        f"Error: {item.get('error', 'unknown')}",
                        title="Task Failed",
                        priority="high",
                    )

                # Archive failed items too
                if not self.dry_run:
                    conn = self.queue._get_conn()
                    conn.execute(
                        "UPDATE work_items SET status = 'archived' WHERE id = ? AND status = 'failed'",
                        (item["id"],),
                    )
                    conn.commit()

    def _update_stats_from_result(self, game_id: str, task_type: str, result: dict):
        """Update registry stats from task result."""
        if task_type == "tile":
            self.registry.update_stats(
                game_id,
                tile_count=result.get("tiles"),
                segment_count=result.get("segments"),
            )
        elif task_type == "label":
            self.registry.update_stats(
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
                    f"R: {metrics.get('recall', 0):.3f}\n"
                    f"Train: {result.get('train_tiles', 0)} tiles, "
                    f"Val: {result.get('val_tiles', 0)} tiles",
                    title="Training Complete",
                )

    # ------------------------------------------------------------------
    # Enqueue work
    # ------------------------------------------------------------------

    def _enqueue_work(self):
        """Scan game states and enqueue appropriate work."""
        games = self.registry.get_games_needing_work()

        for game in games:
            game_id = game["game_id"]
            state = game["pipeline_state"]

            # Skip games with too many failures
            if game.get("pipeline_attempts", 0) >= 3:
                continue

            # Determine what task to enqueue
            task_type = next_task_for_game(state)
            if not task_type:
                continue

            # Don't duplicate
            if self.queue.has_active_item(task_type, game_id):
                continue

            # Resource-specific constraints
            if task_type == "stage":
                # Only one staging at a time (HDD)
                active_stages = self.queue.get_items(status="running", task_type="stage")
                claimed_stages = self.queue.get_items(status="claimed", task_type="stage")
                if len(active_stages) + len(claimed_stages) >= self.cfg.orchestrator.max_staging_concurrent:
                    continue

            if task_type == "sonnet_qa":
                # Rate limit
                if not self._can_qa():
                    continue

            # Build payload
            payload = self._build_payload(game, task_type)

            # Determine priority
            priority = self._get_priority(task_type, game)

            # Determine target machine
            target = self._get_target_machine(task_type)

            if self.dry_run:
                logger.info("[DRY RUN] Would enqueue %s for %s (priority=%d, target=%s)",
                             task_type, game_id, priority, target or "any")
            else:
                self.queue.enqueue(
                    task_type,
                    game_id=game_id,
                    priority=priority,
                    target_machine=target,
                    payload=payload,
                )

        # Check if training is needed
        self._maybe_enqueue_training()

    def _build_payload(self, game: dict, task_type: str) -> dict:
        """Build task-specific payload."""
        payload = {}

        if task_type == "stage":
            payload["video_path"] = game.get("video_path", "")

        if task_type in ("tile", "label"):
            payload["needs_flip"] = bool(game.get("needs_flip"))
            payload["camera_type"] = game.get("camera_type", "dahua")

        return payload

    def _get_priority(self, task_type: str, game: dict) -> int:
        """Determine task priority (lower = higher priority)."""
        base = {
            "ingest_reviews": 5,
            "train": 10,
            "label": 20,
            "measure_coverage": 25,
            "fill_gaps": 30,
            "tile": 35,
            "stage": 40,
            "sonnet_qa": 50,
            "generate_review": 55,
        }.get(task_type, 50)

        # Boost priority for games with partial progress
        if game.get("label_count", 0) > 0:
            base -= 5

        return base

    def _get_target_machine(self, task_type: str) -> str | None:
        """Determine if a task should target a specific machine."""
        if task_type == "stage":
            return self.cfg.server.hostname  # Only server has F: drive
        if task_type == "train":
            # Prefer laptop (best GPU)
            for name, m in self.cfg.machines.items():
                if "train" in m.capabilities:
                    return m.hostname
        return None  # Any capable machine

    def _maybe_enqueue_training(self):
        """Check if we should start a new training run."""
        if self.queue.has_active_item("train"):
            return

        trainable = self.registry.get_trainable_games()
        if len(trainable) < self.cfg.orchestrator.min_new_games_for_retrain:
            return

        # Check how many new labels since last training
        # (simplified: just check if we have enough trainable games)
        train_games = [g["game_id"] for g in trainable]

        # Use first game as validation
        val_games = train_games[:1]
        train_games = train_games[1:]

        if not train_games:
            return

        version = f"v3.{int(time.time()) % 10000}"
        payload = {
            "train_games": train_games,
            "val_games": val_games,
            "version": version,
        }

        if self.dry_run:
            logger.info("[DRY RUN] Would enqueue training %s (%d train, %d val games)",
                         version, len(train_games), len(val_games))
        else:
            self.queue.enqueue(
                "train",
                priority=10,
                target_machine=self._get_target_machine("train"),
                payload=payload,
            )
            self._ntfy(
                f"Training {version} enqueued ({len(train_games)} train, {len(val_games)} val games)",
                title="Training Queued",
            )

    def _can_qa(self) -> bool:
        """Check if we're within Sonnet QA rate limit."""
        now = time.time()
        # Reset counter every hour
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
        """Monitor worker health and alert on problems."""
        workers = self.queue.get_worker_status()
        now = time.time()

        for w in workers:
            age = now - (w.get("last_seen") or 0)

            # Worker hasn't reported in >5 minutes
            if age > 300 and w.get("status") not in ("offline",):
                self._ntfy(
                    f"Worker {w['hostname']} hasn't reported in {age / 60:.0f} min. "
                    f"Last status: {w.get('status', '?')}",
                    title="Worker Down?",
                    priority="high",
                )

            # GPU too hot
            if (w.get("gpu_temp_c") or 0) > 90:
                self._ntfy(
                    f"Worker {w['hostname']} GPU at {w['gpu_temp_c']}C!",
                    title="GPU Overheating",
                    priority="urgent",
                )

            # Disk almost full
            if 0 < (w.get("disk_free_gb") or 999) < 10:
                self._ntfy(
                    f"Worker {w['hostname']} disk low: {w['disk_free_gb']:.1f} GB free",
                    title="Disk Low",
                    priority="high",
                )

    # ------------------------------------------------------------------
    # NTFY
    # ------------------------------------------------------------------

    def _ntfy(self, message: str, title: str = "Pipeline", priority: str = "default"):
        """Send NTFY notification."""
        if not self.cfg.ntfy.enabled or self.dry_run:
            logger.info("[NTFY %s] %s: %s", priority, title, message)
            return

        try:
            subprocess.run(
                [
                    "curl", "-s",
                    "-H", f"Title: {title}",
                    "-H", f"Priority: {priority}",
                    "-d", message,
                    f"https://ntfy.sh/{self.cfg.ntfy.topic}",
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning("NTFY failed: %s", e)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        self.queue.close()
        self.registry.close()
