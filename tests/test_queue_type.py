"""Tests for the QueueType enum."""

from video_grouper.task_processors.queue_type import QueueType


class TestQueueType:
    """Test the QueueType enum."""

    def test_ball_tracking_queue_type_exists(self):
        assert hasattr(QueueType, "BALL_TRACKING")
        assert QueueType.BALL_TRACKING.value == "ball_tracking"

    def test_all_queue_types(self):
        expected_types = {
            "DOWNLOAD": "download",
            "VIDEO": "video",
            "UPLOAD": "upload",
            "NTFY": "ntfy",
            "BALL_TRACKING": "ball_tracking",
        }

        for name, value in expected_types.items():
            assert hasattr(QueueType, name)
            assert getattr(QueueType, name).value == value

    def test_string_representation(self):
        assert QueueType.BALL_TRACKING.value == "ball_tracking"
        assert QueueType.DOWNLOAD.value == "download"
        assert QueueType.VIDEO.value == "video"
        assert QueueType.UPLOAD.value == "upload"
        assert QueueType.NTFY.value == "ntfy"
        assert QueueType.YOUTUBE.value == "youtube"

    def test_enum_comparison(self):
        assert QueueType.BALL_TRACKING == QueueType.BALL_TRACKING
        assert QueueType.BALL_TRACKING != QueueType.DOWNLOAD
        assert QueueType.BALL_TRACKING != QueueType.VIDEO
        assert QueueType.BALL_TRACKING != QueueType.UPLOAD
        assert QueueType.BALL_TRACKING != QueueType.NTFY

    def test_enum_hash(self):
        ball_set = {QueueType.BALL_TRACKING, QueueType.BALL_TRACKING}
        assert len(ball_set) == 1

        all_types_set = {
            QueueType.DOWNLOAD,
            QueueType.VIDEO,
            QueueType.UPLOAD,
            QueueType.NTFY,
            QueueType.BALL_TRACKING,
            QueueType.CLIPS,
        }
        assert len(all_types_set) == 6

    def test_enum_iteration(self):
        all_types = list(QueueType)
        assert len(all_types) == 8
        assert QueueType.BALL_TRACKING in all_types
        assert QueueType.YOUTUBE in all_types
        assert QueueType.CLIPS in all_types
