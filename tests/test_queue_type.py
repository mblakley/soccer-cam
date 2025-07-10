"""Tests for the QueueType enum."""

from video_grouper.task_processors.queue_type import QueueType


class TestQueueType:
    """Test the QueueType enum."""

    def test_autocam_queue_type_exists(self):
        """Test that AUTOCAM queue type exists."""
        assert hasattr(QueueType, "AUTOCAM")
        assert QueueType.AUTOCAM.value == "autocam"

    def test_all_queue_types(self):
        """Test all queue types are present."""
        expected_types = {
            "DOWNLOAD": "download",
            "VIDEO": "video",
            "UPLOAD": "upload",
            "NTFY": "ntfy",
            "AUTOCAM": "autocam",
        }

        for name, value in expected_types.items():
            assert hasattr(QueueType, name)
            assert getattr(QueueType, name).value == value

    def test_string_representation(self):
        """Test string representation of queue types."""
        assert str(QueueType.AUTOCAM) == "autocam"
        assert str(QueueType.DOWNLOAD) == "download"
        assert str(QueueType.VIDEO) == "video"
        assert str(QueueType.UPLOAD) == "upload"
        assert str(QueueType.NTFY) == "ntfy"

    def test_enum_comparison(self):
        """Test enum comparison."""
        assert QueueType.AUTOCAM == QueueType.AUTOCAM
        assert QueueType.AUTOCAM != QueueType.DOWNLOAD
        assert QueueType.AUTOCAM != QueueType.VIDEO
        assert QueueType.AUTOCAM != QueueType.UPLOAD
        assert QueueType.AUTOCAM != QueueType.NTFY

    def test_enum_hash(self):
        """Test enum hash functionality."""
        autocam_set = {QueueType.AUTOCAM, QueueType.AUTOCAM}
        assert len(autocam_set) == 1

        all_types_set = {
            QueueType.DOWNLOAD,
            QueueType.VIDEO,
            QueueType.UPLOAD,
            QueueType.NTFY,
            QueueType.AUTOCAM,
        }
        assert len(all_types_set) == 5

    def test_enum_iteration(self):
        """Test enum iteration."""
        all_types = list(QueueType)
        assert len(all_types) == 5
        assert QueueType.AUTOCAM in all_types
