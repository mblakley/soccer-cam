# End-to-End Test Suite

This directory contains comprehensive end-to-end tests for the Video Grouper system. These tests verify the complete pipeline from file discovery through final processing using mock services and simulated components.

## Overview

The E2E test suite simulates the complete video processing workflow:

1. **Camera Discovery**: Simulator provides 5 video files from 2 hours ago
2. **File Download**: All files downloaded to test data directory
3. **Video Combination**: Files combined into single MP4
4. **Match Info Collection**: Mock TeamSnap/PlayMetrics return overlapping match data
5. **Video Trimming**: Combined file trimmed based on match timing
6. **Autocam Processing**: Mock automation processes the trimmed file
7. **Completion**: Pipeline marked as complete

## Quick Start

### Prerequisites

- Python 3.8+
- `uv` package manager
- `ffmpeg` (optional, for realistic test videos)

### Running Tests

#### Option 1: Standalone Script (Recommended)
```bash
# From project root
uv run python tests/e2e/run_e2e_tests.py
```

#### Option 2: With Pytest
```bash
# From project root
uv run python tests/e2e/run_e2e_tests.py pytest
```

#### Option 3: Direct Pytest
```bash
# From project root
uv run python -m pytest tests/e2e/ -v -m e2e
```

## Test Configuration

The test uses `e2e_test_config.ini` which:
- Enables the camera simulator (`type = simulator`)
- Configures mock services (TeamSnap, PlayMetrics, Autocam)
- Sets test-friendly intervals and timeouts
- Disables YouTube uploads for basic testing

## Test Components

### Mock Services

- **Camera Simulator**: `video_grouper/cameras/simulator.py`
- **Mock TeamSnap**: `video_grouper/api_integrations/mock_teamsnap.py`
- **Mock PlayMetrics**: `video_grouper/api_integrations/mock_playmetrics.py`
- **Mock Autocam**: `video_grouper/tray/mock_autocam_automation.py`

### Test Runner

- **Main Runner**: `test_runner.py` - Orchestrates the complete test
- **Process Management**: Starts video_grouper and tray as subprocesses
- **Progress Monitoring**: Tracks pipeline completion through state files
- **Cleanup**: Ensures proper process termination and file cleanup

## Test Data

Test data is stored in:
- **Test Data**: `tests/e2e/test_data/` (cleaned up after test)
- **Test Logs**: `tests/e2e/test_logs/`

## Environment Variables

The test automatically sets these environment variables:
- `USE_MOCK_AUTOCAM=true`
- `USE_MOCK_TEAMSNAP=true`
- `USE_MOCK_PLAYMETRICS=true`
- `USE_MOCK_SERVICES=true`

## Monitoring Progress

The test runner provides real-time progress updates showing:
- Directories found
- Files discovered and downloaded
- Video combination status
- Match info collection
- Trimming and autocam completion

## Troubleshooting

### Common Issues

1. **Process fails to start**: Check that `uv` and Python are available
2. **Test times out**: Increase `max_wait_minutes` in test runner
3. **Mock services not working**: Verify environment variables are set
4. **Permission errors**: Ensure write access to test directories

### Debug Mode

For detailed debugging, run with increased logging:
```bash
uv run python tests/e2e/run_e2e_tests.py --log-level=DEBUG
```

### Manual Inspection

To inspect test artifacts before cleanup:
```python
# In test_runner.py, comment out cleanup in finally block
# self.cleanup_test_environment()
```

## Extending Tests

### Adding New Test Scenarios

1. Create new test methods in `TestE2EPipeline`
2. Configure different mock data in simulator classes
3. Add new validation steps in `_is_pipeline_complete`

### Custom Mock Services

1. Implement mock service following existing patterns
2. Add factory function in `mock_services.py`
3. Update environment variable handling

## Integration with CI/CD

The E2E tests can be integrated into CI/CD pipelines:

```yaml
# Example GitHub Actions step
- name: Run E2E Tests
  run: |
    uv run python tests/e2e/run_e2e_tests.py
```

## Performance Considerations

- Tests typically complete in 5-15 minutes
- Mock services reduce external dependencies
- Test data is automatically cleaned up
- Processes are properly terminated to prevent resource leaks 