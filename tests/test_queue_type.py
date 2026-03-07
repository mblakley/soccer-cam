"""Tests for the QueueType enum."""

from video_grouper.task_processors.queue_type import QueueType


class TestQueueType:
    """Test the QueueType enum."""

    def test_tracking_queue_type_exists(self):
        """Test that TRACKING queue type exists."""
        assert hasattr(QueueType, "TRACKING")
        assert QueueType.TRACKING.value == "tracking"

    def test_all_queue_types(self):
        """Test all queue types are present."""
        expected_types = {
            "DOWNLOAD": "download",
            "VIDEO": "video",
            "UPLOAD": "upload",
            "NTFY": "ntfy",
            "TRACKING": "tracking",
        }

        for name, value in expected_types.items():
            assert hasattr(QueueType, name)
            assert getattr(QueueType, name).value == value

    def test_string_representation(self):
        """Test value representation of queue types."""
        assert QueueType.TRACKING.value == "tracking"
        assert QueueType.DOWNLOAD.value == "download"
        assert QueueType.VIDEO.value == "video"
        assert QueueType.UPLOAD.value == "upload"
        assert QueueType.NTFY.value == "ntfy"
        assert QueueType.YOUTUBE.value == "youtube"

    def test_enum_comparison(self):
        """Test enum comparison."""
        assert QueueType.TRACKING == QueueType.TRACKING
        assert QueueType.TRACKING != QueueType.DOWNLOAD
        assert QueueType.TRACKING != QueueType.VIDEO
        assert QueueType.TRACKING != QueueType.UPLOAD
        assert QueueType.TRACKING != QueueType.NTFY

    def test_enum_hash(self):
        """Test enum hash functionality."""
        tracking_set = {QueueType.TRACKING, QueueType.TRACKING}
        assert len(tracking_set) == 1

        all_types_set = {
            QueueType.DOWNLOAD,
            QueueType.VIDEO,
            QueueType.UPLOAD,
            QueueType.NTFY,
            QueueType.TRACKING,
        }
        assert len(all_types_set) == 5

    def test_enum_iteration(self):
        """Test enum iteration."""
        all_types = list(QueueType)
        assert len(all_types) == 7
        assert QueueType.TRACKING in all_types
        assert QueueType.YOUTUBE in all_types
