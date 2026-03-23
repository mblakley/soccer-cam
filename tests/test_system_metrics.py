"""Tests for system metrics collection."""

from video_grouper.utils.system_metrics import get_system_metrics, get_disk_free_gb


def test_get_system_metrics_returns_dict():
    metrics = get_system_metrics()
    # May be empty if psutil not installed, but should not raise
    assert isinstance(metrics, dict)


def test_get_system_metrics_keys_when_available():
    """If psutil is available, verify expected keys are present."""
    metrics = get_system_metrics()
    if metrics:  # Non-empty means psutil is installed
        assert "cpu_usage_percent" in metrics
        assert "memory_usage_percent" in metrics
        assert "disk_free_gb" in metrics
        assert "disk_total_gb" in metrics
        assert "uptime_seconds" in metrics


def test_get_system_metrics_value_types():
    """Verify value types are correct when metrics are available."""
    metrics = get_system_metrics()
    if metrics:
        assert isinstance(metrics["cpu_usage_percent"], float)
        assert isinstance(metrics["memory_usage_percent"], float)
        assert isinstance(metrics["disk_free_gb"], float)
        assert isinstance(metrics["disk_total_gb"], float)
        assert isinstance(metrics["uptime_seconds"], int)


def test_get_disk_free_gb():
    result = get_disk_free_gb()
    # May be None if psutil not installed
    if result is not None:
        assert result > 0
        assert isinstance(result, float)


def test_get_disk_free_gb_with_path():
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        result = get_disk_free_gb(tmpdir)
        if result is not None:
            assert result > 0


def test_get_system_metrics_does_not_raise():
    """Calling get_system_metrics should never raise an exception."""
    try:
        get_system_metrics()
    except Exception as exc:
        raise AssertionError(f"get_system_metrics raised: {exc}") from exc


def test_get_disk_free_gb_does_not_raise():
    """Calling get_disk_free_gb should never raise an exception."""
    try:
        get_disk_free_gb()
        get_disk_free_gb("/nonexistent_path_xyz")
    except Exception as exc:
        raise AssertionError(f"get_disk_free_gb raised: {exc}") from exc
