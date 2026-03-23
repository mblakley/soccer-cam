"""Tests for the in-memory error ring buffer."""

from video_grouper.utils.error_tracker import ErrorTracker


def test_record_and_get_last():
    t = ErrorTracker()
    t.record("download", "timeout")
    assert t.get_last_error() == "[download] timeout"


def test_empty_tracker():
    t = ErrorTracker()
    assert t.get_last_error() is None
    assert t.get_error_count_24h() == 0


def test_error_count_24h():
    t = ErrorTracker()
    t.record("a", "err1")
    t.record("b", "err2")
    assert t.get_error_count_24h() == 2


def test_recent_errors_order():
    t = ErrorTracker()
    t.record("a", "first")
    t.record("b", "second")
    recent = t.get_recent_errors()
    assert recent[0]["message"] == "second"  # Newest first


def test_max_errors_ring_buffer():
    t = ErrorTracker(max_errors=3)
    t.record("a", "1")
    t.record("b", "2")
    t.record("c", "3")
    t.record("d", "4")
    assert len(t.get_recent_errors(10)) == 3
    assert t.get_recent_errors()[0]["message"] == "4"


def test_clear():
    t = ErrorTracker()
    t.record("a", "err")
    t.clear()
    assert t.get_error_count_24h() == 0
    assert t.get_last_error() is None


def test_record_with_context():
    t = ErrorTracker()
    t.record("download", "timeout", {"camera_ip": "192.168.1.100"})
    recent = t.get_recent_errors()
    assert recent[0]["context"]["camera_ip"] == "192.168.1.100"


def test_get_recent_errors_limit():
    t = ErrorTracker(max_errors=50)
    for i in range(10):
        t.record("stage", f"error {i}")
    recent = t.get_recent_errors(limit=3)
    assert len(recent) == 3
    # Most recent first
    assert recent[0]["message"] == "error 9"


def test_to_dict_fields():
    t = ErrorTracker()
    t.record("upload", "failed", {"file": "game.mp4"})
    entry = t.get_recent_errors()[0]
    assert "stage" in entry
    assert "message" in entry
    assert "context" in entry
    assert "timestamp" in entry
    assert entry["stage"] == "upload"
