"""Tests for the annotation server, review packet generator, and correction ingester."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from training.annotation_server import REVIEW_PACKETS_DIR, app


@pytest.fixture()
def review_packets_dir(tmp_path, monkeypatch):
    """Set up a temporary review packets directory with test data."""
    packets_dir = tmp_path / "review_packets"
    packets_dir.mkdir()

    # Patch the module-level constant
    import training.annotation_server as mod

    monkeypatch.setattr(mod, "REVIEW_PACKETS_DIR", packets_dir)

    return packets_dir


@pytest.fixture()
def sample_packet(review_packets_dir):
    """Create a sample review packet with manifest and crop images."""
    game_dir = review_packets_dir / "test_game_01"
    crops_dir = game_dir / "crops"
    crops_dir.mkdir(parents=True)

    manifest = {
        "game_id": "test_game_01",
        "model_version": "ball_v1",
        "source_video": "/fake/video.mp4",
        "source_resolution": {"w": 4096, "h": 1800},
        "total_game_frames": 5000,
        "frames": [
            {
                "frame_idx": 100,
                "crop_file": "crops/frame_000100.jpg",
                "crop_origin": {"x": 500, "y": 300, "w": 640, "h": 640},
                "source_resolution": {"w": 4096, "h": 1800},
                "model_detection": {"x": 320, "y": 320, "confidence": 0.35},
                "reason": "low_confidence",
                "context": {"tracker_state": "ball", "play_region_yaw": 0.5},
            },
            {
                "frame_idx": 200,
                "crop_file": "crops/frame_000200.jpg",
                "crop_origin": {"x": 1000, "y": 400, "w": 640, "h": 640},
                "source_resolution": {"w": 4096, "h": 1800},
                "model_detection": None,
                "reason": "tracker_lost",
                "context": {"tracker_state": "play_region", "play_region_yaw": 1.2},
            },
            {
                "frame_idx": 300,
                "crop_file": "crops/frame_000300.jpg",
                "crop_origin": {"x": 2000, "y": 500, "w": 640, "h": 640},
                "source_resolution": {"w": 4096, "h": 1800},
                "model_detection": {"x": 100, "y": 200, "confidence": 0.85},
                "reason": "high_confidence_audit",
                "context": {"tracker_state": "ball", "play_region_yaw": 2.0},
            },
        ],
    }

    with open(game_dir / "manifest.json", "w") as f:
        json.dump(manifest, f)

    # Create minimal valid JPEG files (SOI + EOI markers)
    for idx in [100, 200, 300]:
        (crops_dir / f"frame_{idx:06d}.jpg").write_bytes(b"\xff\xd8\xff\xd9")

    return game_dir


@pytest.fixture()
def client(review_packets_dir):
    """FastAPI test client."""
    return TestClient(app)


class TestListPackets:
    def test_empty_directory(self, client):
        resp = client.get("/api/packets")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_lists_packet(self, client, sample_packet):
        resp = client.get("/api/packets")
        assert resp.status_code == 200
        packets = resp.json()
        assert len(packets) == 1
        assert packets[0]["game_id"] == "test_game_01"
        assert packets[0]["frame_count"] == 3
        assert packets[0]["reviewed_count"] == 0
        assert packets[0]["status"] == "pending"


class TestGetPacket:
    def test_get_existing_packet(self, client, sample_packet):
        resp = client.get("/api/packets/test_game_01")
        assert resp.status_code == 200
        data = resp.json()
        assert data["game_id"] == "test_game_01"
        assert len(data["frames"]) == 3
        assert data["status"] == "pending"
        assert data["reviewed_count"] == 0
        assert data["reviewed_frames"] == []

    def test_get_nonexistent_packet(self, client, sample_packet):
        resp = client.get("/api/packets/nonexistent")
        assert resp.status_code == 404


class TestGetCrop:
    def test_get_valid_crop(self, client, sample_packet):
        resp = client.get("/api/packets/test_game_01/crops/100")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert len(resp.content) > 0

    def test_get_invalid_frame_idx(self, client, sample_packet):
        resp = client.get("/api/packets/test_game_01/crops/999")
        assert resp.status_code == 404


class TestSubmitResults:
    def test_submit_single_result(self, client, sample_packet):
        resp = client.post(
            "/api/packets/test_game_01/results",
            json={
                "results": [
                    {
                        "frame_idx": 100,
                        "action": "confirm",
                        "duration_ms": 1500,
                    }
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 1
        assert data["total_reviewed"] == 1
        assert data["total_frames"] == 3
        assert data["status"] == "partial"

    def test_submit_all_results_marks_complete(self, client, sample_packet):
        resp = client.post(
            "/api/packets/test_game_01/results",
            json={
                "results": [
                    {"frame_idx": 100, "action": "confirm", "duration_ms": 1000},
                    {
                        "frame_idx": 200,
                        "action": "locate",
                        "ball_position": {"x": 400, "y": 350},
                        "duration_ms": 3000,
                    },
                    {"frame_idx": 300, "action": "reject", "duration_ms": 800},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_reviewed"] == 3
        assert data["status"] == "complete"

    def test_incremental_submission_merges(self, client, sample_packet):
        # Submit first result
        client.post(
            "/api/packets/test_game_01/results",
            json={"results": [{"frame_idx": 100, "action": "confirm"}]},
        )
        # Submit second result
        resp = client.post(
            "/api/packets/test_game_01/results",
            json={"results": [{"frame_idx": 200, "action": "reject"}]},
        )
        data = resp.json()
        assert data["total_reviewed"] == 2

    def test_resubmit_overwrites_previous(self, client, sample_packet):
        # Submit confirm
        client.post(
            "/api/packets/test_game_01/results",
            json={"results": [{"frame_idx": 100, "action": "confirm"}]},
        )
        # Change to reject
        client.post(
            "/api/packets/test_game_01/results",
            json={"results": [{"frame_idx": 100, "action": "reject"}]},
        )
        # Still only 1 reviewed (overwritten, not duplicated)
        resp = client.get("/api/packets/test_game_01")
        assert resp.json()["reviewed_count"] == 1

    def test_submit_to_nonexistent_packet(self, client, sample_packet):
        resp = client.post(
            "/api/packets/nonexistent/results",
            json={"results": [{"frame_idx": 1, "action": "confirm"}]},
        )
        assert resp.status_code == 404


class TestSkipPacket:
    def test_skip_creates_marker(self, client, sample_packet):
        resp = client.post("/api/packets/test_game_01/skip")
        assert resp.status_code == 200
        assert (sample_packet / ".skipped").exists()


class TestStats:
    def test_empty_stats(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_packets"] == 0
        assert data["total_reviewed"] == 0

    def test_stats_after_review(self, client, sample_packet):
        # Submit some results
        client.post(
            "/api/packets/test_game_01/results",
            json={
                "results": [
                    {"frame_idx": 100, "action": "confirm", "duration_ms": 1500},
                    {"frame_idx": 200, "action": "reject", "duration_ms": 2000},
                ]
            },
        )
        resp = client.get("/api/stats")
        data = resp.json()
        assert data["total_packets"] == 1
        assert data["total_frames"] == 3
        assert data["total_reviewed"] == 2
        assert data["action_breakdown"]["confirm"] == 1
        assert data["action_breakdown"]["reject"] == 1
        assert data["total_review_time_minutes"] == pytest.approx(3500 / 60000, abs=0.1)


class TestCorrectionIngester:
    def test_ingest_confirm(self, sample_packet, tmp_path):
        from training.correction_ingester import ingest_annotations

        # Write annotation results
        results = [
            {
                "frame_idx": 100,
                "action": "confirm",
                "ball_position": None,
                "duration_ms": 1000,
                "reviewer": "phone",
                "submitted_at": "2025-01-01T00:00:00Z",
            }
        ]
        with open(sample_packet / "annotation_results.json", "w") as f:
            json.dump(results, f)

        labels_dir = tmp_path / "labels"
        stats = ingest_annotations(sample_packet, labels_dir)

        assert stats.confirmed == 1
        assert stats.labels_written == 1
        label_file = labels_dir / "test_game_01_frame_000100.txt"
        assert label_file.exists()
        content = label_file.read_text().strip()
        parts = content.split()
        assert parts[0] == "0"  # class
        assert len(parts) == 5  # class cx cy w h

    def test_ingest_reject_writes_empty_label(self, sample_packet, tmp_path):
        from training.correction_ingester import ingest_annotations

        results = [
            {
                "frame_idx": 200,
                "action": "reject",
                "ball_position": None,
                "duration_ms": 500,
                "reviewer": "phone",
                "submitted_at": "2025-01-01T00:00:00Z",
            }
        ]
        with open(sample_packet / "annotation_results.json", "w") as f:
            json.dump(results, f)

        labels_dir = tmp_path / "labels"
        stats = ingest_annotations(sample_packet, labels_dir)

        assert stats.rejected == 1
        assert stats.labels_written == 1
        label_file = labels_dir / "test_game_01_frame_000200.txt"
        assert label_file.exists()
        assert label_file.read_text() == ""  # empty = negative example

    def test_ingest_adjust_with_position(self, sample_packet, tmp_path):
        from training.correction_ingester import ingest_annotations

        results = [
            {
                "frame_idx": 100,
                "action": "adjust",
                "ball_position": {"x": 350, "y": 400},
                "duration_ms": 2000,
                "reviewer": "phone",
                "submitted_at": "2025-01-01T00:00:00Z",
            }
        ]
        with open(sample_packet / "annotation_results.json", "w") as f:
            json.dump(results, f)

        labels_dir = tmp_path / "labels"
        stats = ingest_annotations(sample_packet, labels_dir)

        assert stats.adjusted == 1
        assert stats.labels_written == 1
        label_file = labels_dir / "test_game_01_frame_000100.txt"
        content = label_file.read_text().strip().split()
        # Verify position: crop_origin x=500, y=300 + adjust x=350, y=400
        # full_x=850, full_y=700, img_w=4096, img_h=1800
        cx = float(content[1])
        cy = float(content[2])
        assert cx == pytest.approx(850 / 4096, abs=0.001)
        assert cy == pytest.approx(700 / 1800, abs=0.001)

    def test_ingest_locate_with_position(self, sample_packet, tmp_path):
        from training.correction_ingester import ingest_annotations

        results = [
            {
                "frame_idx": 200,
                "action": "locate",
                "ball_position": {"x": 200, "y": 150},
                "duration_ms": 4000,
                "reviewer": "phone",
                "submitted_at": "2025-01-01T00:00:00Z",
            }
        ]
        with open(sample_packet / "annotation_results.json", "w") as f:
            json.dump(results, f)

        labels_dir = tmp_path / "labels"
        stats = ingest_annotations(sample_packet, labels_dir)
        assert stats.located == 1
        assert stats.labels_written == 1

    def test_ingest_not_visible(self, sample_packet, tmp_path):
        from training.correction_ingester import ingest_annotations

        results = [
            {
                "frame_idx": 300,
                "action": "not_visible",
                "ball_position": None,
                "duration_ms": 500,
                "reviewer": "phone",
                "submitted_at": "2025-01-01T00:00:00Z",
            }
        ]
        with open(sample_packet / "annotation_results.json", "w") as f:
            json.dump(results, f)

        labels_dir = tmp_path / "labels"
        stats = ingest_annotations(sample_packet, labels_dir)
        assert stats.not_visible == 1
        label_file = labels_dir / "test_game_01_frame_000300.txt"
        assert label_file.read_text() == ""  # empty = negative example

    def test_ingest_skip_writes_nothing(self, sample_packet, tmp_path):
        from training.correction_ingester import ingest_annotations

        results = [
            {
                "frame_idx": 100,
                "action": "skip",
                "ball_position": None,
                "duration_ms": 200,
                "reviewer": "phone",
                "submitted_at": "2025-01-01T00:00:00Z",
            }
        ]
        with open(sample_packet / "annotation_results.json", "w") as f:
            json.dump(results, f)

        labels_dir = tmp_path / "labels"
        stats = ingest_annotations(sample_packet, labels_dir)
        assert stats.skipped == 1
        assert stats.labels_written == 0


class TestIngestAllPackets:
    def test_processes_completed_packets(self, review_packets_dir, sample_packet, tmp_path):
        from training.correction_ingester import ingest_all_packets

        # Add annotation results to make the packet "completed"
        results = [
            {
                "frame_idx": 100,
                "action": "confirm",
                "ball_position": None,
                "duration_ms": 1000,
                "reviewer": "phone",
                "submitted_at": "2025-01-01T00:00:00Z",
            }
        ]
        with open(sample_packet / "annotation_results.json", "w") as f:
            json.dump(results, f)

        labels_dir = tmp_path / "labels"
        stats_list = ingest_all_packets(review_packets_dir, labels_dir)

        assert len(stats_list) == 1
        assert stats_list[0].confirmed == 1

        # Check .ingested marker was created
        assert (sample_packet / ".ingested").exists()

    def test_skips_already_ingested(self, review_packets_dir, sample_packet, tmp_path):
        from training.correction_ingester import ingest_all_packets

        results = [{"frame_idx": 100, "action": "confirm", "ball_position": None,
                     "duration_ms": 1000, "reviewer": "phone",
                     "submitted_at": "2025-01-01T00:00:00Z"}]
        with open(sample_packet / "annotation_results.json", "w") as f:
            json.dump(results, f)

        labels_dir = tmp_path / "labels"
        # First run
        ingest_all_packets(review_packets_dir, labels_dir)
        # Second run should skip
        stats_list = ingest_all_packets(review_packets_dir, labels_dir)
        assert len(stats_list) == 0


class TestIngestionEndpoint:
    def test_ingest_via_api(self, client, sample_packet, tmp_path, monkeypatch):
        import training.annotation_server as mod

        monkeypatch.setattr(mod, "LABELS_OUTPUT_DIR", tmp_path / "labels")

        # Add annotation results
        results = [
            {
                "frame_idx": 100,
                "action": "confirm",
                "ball_position": None,
                "duration_ms": 1000,
                "reviewer": "phone",
                "submitted_at": "2025-01-01T00:00:00Z",
            }
        ]
        with open(sample_packet / "annotation_results.json", "w") as f:
            json.dump(results, f)

        resp = client.post("/api/ingest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["packets_processed"] == 1
        assert data["total_labels_written"] == 1
