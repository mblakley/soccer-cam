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
    get_failed_stage,
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
        self._alerted_stuck: set[str] = (
            set()
        )  # games that hit hard limit (notified once)
        self._last_reset: dict[
            str, float
        ] = {}  # game_id -> timestamp of last auto-reset
        self._qa_exhausted: set[str] = (
            set()
        )  # games where sonnet_qa found no candidates (skip re-enqueue)

    def run(self, once: bool = False):
        """Main orchestrator loop. Requires API server to be running separately."""
        logger.info(
            "Orchestrator starting (interval=%ds, dry_run=%s)",
            self.cfg.orchestrator.check_interval,
            self.dry_run,
        )

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
        self._audit_game_states()
        self._enqueue_work()
        # Periodic WAL checkpoint to prevent unbounded WAL growth
        self.api.maybe_checkpoint()

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
                        # New labels may exist — allow QA re-check
                        self._qa_exhausted.discard(game_id)
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
                                self._update_stats_from_result(
                                    game_id, task_type, result
                                )

                        logger.info(
                            "Game %s: %s -> %s (via %s)",
                            game_id,
                            current_state,
                            new_state,
                            task_type,
                        )
                        self.api.log_event(
                            "info",
                            f"{game_id} {current_state} -> {new_state}",
                            category="state_change",
                            game_id=game_id,
                        )

            # Track QA-exhausted games to avoid re-enqueue spam
            if task_type == "sonnet_qa" and game_id:
                result = item.get("result")
                if isinstance(result, str):
                    try:
                        result_data = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        result_data = {}
                else:
                    result_data = result or {}
                if result_data.get("tiles_reviewed", -1) == 0:
                    self._qa_exhausted.add(game_id)
                    logger.debug(
                        "QA exhausted for %s — no candidates remain", game_id
                    )

            # Archive processed items so they don't get re-processed
            if not self.dry_run:
                self.api.archive(item["id"])

        # Check failed items — set game state and archive
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
                            self.api.set_game_state(
                                game_id, new_state, error=item.get("error")
                            )
                            self.api.increment_attempts(game_id)

                        # Notify once when a game hits 20 attempts (hard limit)
                        game_info = self.api.get_game(game_id)
                        attempts = (
                            game_info.get("pipeline_attempts", 0) if game_info else 0
                        )
                        if attempts >= 20 and game_id not in self._alerted_stuck:
                            self._alerted_stuck.add(game_id)
                            self._ntfy(
                                f"Game stuck after {attempts} attempts: {game_id}\n"
                                f"Error: {item.get('error', 'unknown')[:200]}",
                                title="Game Stuck",
                                priority="default",
                            )

            # Always archive failed items so they don't pile up
            if not self.dry_run:
                self.api.archive(item["id"])

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
        elif task_type == "sonnet_qa":
            coverage = result.get("track_coverage")
            if coverage is not None:
                self.api.update_game_stats(game_id, coverage=coverage)
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
    # Audit — validate game states match actual data
    # ------------------------------------------------------------------

    def _audit_game_states(self):
        """Check that each game's state matches its actual data.

        Demotes games whose state claims more progress than their data
        supports. Also cleans up FAILED:TRAINABLE (terminal state shouldn't fail)
        and prunes stale worker entries. Runs every tick.
        """
        if self.dry_run:
            return

        # Clean stale worker entries (> 24 hours old)
        # Skip if the same hostname also has a fresh entry (WAL corruption
        # can cause duplicate rows despite PRIMARY KEY constraint).
        status = self.api.get_status()
        if status:
            now = time.time()
            workers = status.get("workers", [])
            fresh_hostnames = {
                w["hostname"]
                for w in workers
                if (now - (w.get("last_seen") or 0)) < 86400
            }
            for w in workers:
                age = now - (w.get("last_seen") or 0)
                if age > 86400 and w["hostname"] not in fresh_hostnames:
                    try:
                        import urllib.request

                        req = urllib.request.Request(
                            f"http://127.0.0.1:8643/api/workers/{w['hostname']}",
                            method="DELETE",
                        )
                        urllib.request.urlopen(req)
                        logger.info(
                            "Audit: cleaned stale worker %s (%d hrs old)",
                            w["hostname"],
                            age // 3600,
                        )
                    except Exception:
                        pass

        games = self.api.get_games_needing_work()
        for game in games:
            game_id = game["game_id"]
            state = game["pipeline_state"]
            tiles = game.get("tile_count", 0)
            labels = game.get("label_count", 0)

            # FAILED:TRAINABLE — just reset to TRAINABLE + clear attempts
            if state == "FAILED:TRAINABLE":
                self.api.set_game_state(game_id, "TRAINABLE")
                self.api.reset_attempts(game_id)
                logger.info("Audit: fixed %s FAILED:TRAINABLE->TRAINABLE", game_id)
                continue

            if is_failed(state):
                state = get_failed_stage(state)

            # LABELED or beyond but has 0 tiles — needs tiling
            if state in ("LABELED", "QA_PENDING", "QA_DONE") and tiles == 0:
                self.api.set_game_state(game_id, "TILED")
                logger.info("Audit: demoted %s %s->TILED (0 tiles)", game_id, state)
                continue

            # LABELED but has 0 labels — needs labeling
            if (
                state in ("LABELED", "QA_PENDING", "QA_DONE")
                and labels == 0
                and tiles > 0
            ):
                self.api.set_game_state(game_id, "TILED")
                logger.info("Audit: demoted %s %s->TILED (0 labels)", game_id, state)
                continue

            # TILED but has 0 tiles — needs staging
            if state == "TILED" and tiles == 0:
                self.api.set_game_state(game_id, "STAGING")
                logger.info("Audit: demoted %s TILED->STAGING (0 tiles)", game_id)
                continue

    # ------------------------------------------------------------------
    # Enqueue work
    # ------------------------------------------------------------------

    def _enqueue_work(self):
        games = self.api.get_games_needing_work()

        for game in games:
            game_id = game["game_id"]
            state = game["pipeline_state"]

            attempts = game.get("pipeline_attempts", 0)

            # Auto-recover FAILED games with cooldown.
            # Most failures are transient (disk full, USB hiccup).
            # Wait 5 min between retries, hard limit at 20 attempts.
            if is_failed(state):
                if attempts >= 20:
                    continue  # truly stuck, needs manual investigation

                last_reset = self._last_reset.get(game_id, 0)
                if time.time() - last_reset < 300:
                    continue  # 5 min cooldown between retries

                failed_stage = get_failed_stage(state)
                if failed_stage and not self.dry_run:
                    self.api.set_game_state(game_id, failed_stage)
                    self._last_reset[game_id] = time.time()
                    logger.info("Auto-reset %s (attempt %d)", game_id, attempts)
                continue

            task_type = next_task_for_game(state)
            if not task_type:
                continue

            if self.api.has_active_item(task_type, game_id):
                continue

            if task_type == "stage":
                active_stages = self.api.get_queue_items(status="running")
                claimed_stages = self.api.get_queue_items(status="claimed")
                stage_count = sum(
                    1
                    for i in active_stages + claimed_stages
                    if i.get("task_type") == "stage"
                )
                if stage_count >= self.cfg.orchestrator.max_staging_concurrent:
                    continue

            if task_type == "sonnet_qa" and not self._can_qa():
                continue

            payload = self._build_payload(game, task_type)
            priority = self._get_priority(task_type, game)
            target = self._get_target_machine(task_type)

            if self.dry_run:
                logger.info(
                    "[DRY RUN] Would enqueue %s for %s (priority=%d, target=%s)",
                    task_type,
                    game_id,
                    priority,
                    target or "any",
                )
            else:
                self.api.enqueue(
                    task_type,
                    game_id=game_id,
                    priority=priority,
                    target_machine=target,
                    payload=payload,
                )

        self._maybe_enqueue_training()
        self._maybe_enqueue_continuous_qa()

    def _maybe_enqueue_continuous_qa(self):
        """Keep QA running on any game with un-QA'd labeled tiles.

        QA should never stop — if there are tiles with labels but no
        qa_verdict, enqueue another QA pass. This covers games in
        QA_DONE and TRAINABLE that still have unreviewed tiles.

        Uses its own game list (not just needing-work) since TRAINABLE
        games still need QA on unreviewed tiles.
        """
        if self.api.has_active_item("sonnet_qa"):
            return  # already running one

        # Don't starve generate_review — if one is queued, let it run first
        if self.api.has_active_item("generate_review"):
            return

        # Get ALL games, not just needing-work (which excludes TRAINABLE)
        all_games = self.api.get_all_games()
        qa_eligible_states = {"LABELED", "QA_PENDING", "QA_DONE", "TRAINABLE"}
        for game in all_games:
            state = game["pipeline_state"]
            if state not in qa_eligible_states:
                continue
            if game.get("label_count", 0) == 0:
                continue
            if game["game_id"] in self._qa_exhausted:
                continue  # no candidates last time — don't spam
            if self.api.has_active_item("sonnet_qa", game["game_id"]):
                continue

            # Enqueue QA — the task itself checks for un-QA'd tiles
            # and returns early if there's nothing to do
            if not self.dry_run:
                self.api.enqueue(
                    "sonnet_qa",
                    game_id=game["game_id"],
                    priority=self._get_priority("sonnet_qa", game),
                    target_machine=self._get_target_machine("sonnet_qa"),
                )
                logger.info(
                    "Continuous QA: enqueued for %s (%s)", game["game_id"], state
                )
            break  # One at a time to stay within rate limit

    def _build_payload(self, game: dict, task_type: str) -> dict:
        payload = {}
        if task_type == "stage":
            payload["video_path"] = game.get("video_path", "")
        if task_type in ("tile", "label"):
            payload["needs_flip"] = bool(game.get("needs_flip"))
            payload["camera_type"] = game.get("camera_type", "dahua")
        return payload

    def _get_priority(self, task_type: str, game: dict) -> int:
        # Lower number = higher priority. Designed so QA and review
        # interleave with tiling rather than waiting for all tiles.
        base = {
            "ingest_reviews": 5,
            "generate_review": 10,
            "train": 15,
            "sonnet_qa": 25,  # between label and tile — runs as games become LABELED
            "label": 30,
            "tile": 35,
            "stage": 40,
        }.get(task_type, 50)
        if game.get("label_count", 0) > 0:
            base -= 5
        return base

    def _get_target_machine(self, task_type: str) -> str | None:
        # Server-only tasks (need local F:/D: access for video/pack staging)
        # Note: sonnet_qa, generate_review, and ingest_reviews are NOT
        # targeted — the QA worker has hostname DESKTOP-5L867J8-QA and
        # must be able to claim these tasks.
        if task_type in (
            "stage",
            "tile",
        ):
            return self.cfg.server.hostname
        if task_type == "train":
            for name, m in self.cfg.machines.items():
                if "train" in m.capabilities:
                    return m.hostname
        return None

    def _maybe_enqueue_training(self):
        """Enqueue training driven by ball track coverage.

        The flywheel: retrain when track coverage is below target and
        still improving. Stop when converged or plateaued (need human
        reviews instead). Falls back to game-count trigger when no
        coverage data exists yet (bootstrap).
        """
        if self.api.has_active_item("train"):
            return

        trainable = self.api.get_trainable_games()
        if len(trainable) < self.cfg.orchestrator.min_new_games_for_retrain:
            return

        # Coverage-aware trigger: check if retraining would help
        games_with_coverage = [g for g in trainable if g.get("coverage", 0) > 0]

        if games_with_coverage:
            avg_coverage = sum(g["coverage"] for g in games_with_coverage) / len(
                games_with_coverage
            )

            if avg_coverage >= self.cfg.orchestrator.coverage_target:
                logger.info(
                    "Track coverage converged at %.1f%% (target %.0f%%) — skipping training",
                    avg_coverage * 100,
                    self.cfg.orchestrator.coverage_target * 100,
                )
                return

            prev_coverage = getattr(self, "_last_train_coverage", 0.0)
            delta = avg_coverage - prev_coverage
            if prev_coverage > 0 and delta < self.cfg.orchestrator.coverage_min_delta:
                logger.info(
                    "Track coverage plateaued at %.1f%% (delta=%.1f%%) — need human reviews, skipping training",
                    avg_coverage * 100,
                    delta * 100,
                )
                return
        # else: no coverage data yet, fall through to bootstrap trigger

        # Rate limit: at most 1 training run per hour
        if (
            hasattr(self, "_last_train_time")
            and time.time() - self._last_train_time < 3600
        ):
            return

        self._enqueue_train(trainable)

    def _enqueue_train(self, trainable: list[dict]):
        """Build and enqueue a training task from trainable games."""
        self._last_train_time = time.time()

        game_ids = [g["game_id"] for g in trainable]
        val_games = game_ids[:1]
        train_games = game_ids[1:]
        if not train_games:
            return

        # Track coverage for plateau detection on next cycle
        games_with_coverage = [g for g in trainable if g.get("coverage", 0) > 0]
        if games_with_coverage:
            self._last_train_coverage = sum(
                g["coverage"] for g in games_with_coverage
            ) / len(games_with_coverage)

        # Find previous best weights to resume from (incremental training)
        resume_from = None
        training_sets = Path(self.cfg.paths.training_sets)
        if training_sets.exists():
            versions = sorted(training_sets.iterdir(), reverse=True)
            for v in versions:
                best = v / "weights" / "best.pt"
                if best.exists():
                    resume_from = str(best)
                    break

        version = f"v3.{int(time.time()) % 10000}"
        if self.dry_run:
            logger.info(
                "[DRY RUN] Would enqueue training %s (resume=%s)", version, resume_from
            )
        else:
            self.api.enqueue(
                "train",
                priority=10,
                target_machine=self._get_target_machine("train"),
                payload={
                    "train_games": train_games,
                    "val_games": val_games,
                    "version": version,
                    "resume_from": resume_from,
                },
            )
            logger.info(
                "Enqueued training %s: %d games, resume=%s",
                version,
                len(train_games),
                Path(resume_from).parent.parent.name if resume_from else "scratch",
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
                [
                    "curl",
                    "-s",
                    "-H",
                    f"Title: {title}",
                    "-H",
                    f"Priority: {priority}",
                    "-d",
                    message,
                    f"https://ntfy.sh/{self.cfg.ntfy.topic}",
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning("NTFY failed: %s", e)
