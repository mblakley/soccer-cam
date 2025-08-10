#!/usr/bin/env python3
"""
Simple E2E Test Runner
"""

import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


def setup_test_environment():
    """Set up environment variables for E2E testing."""
    # Set environment variables - simple and direct
    os.environ.update(
        {
            "USE_MOCK_NTFY": "false",  # Use real NTFY API for sending requests
            "USE_MOCK_TEAMSNAP": "true",
            "USE_MOCK_PLAYMETRICS": "true",
            "USE_MOCK_SERVICES": "true",
            "PYTHONPATH": f"{os.environ.get('PYTHONPATH', '')}:{project_root}",
        }
    )
    print("✅ Test environment configured")
    print("  - USE_MOCK_NTFY: false (using real NTFY API)")
    print("  - USE_MOCK_TEAMSNAP: true")
    print("  - USE_MOCK_PLAYMETRICS: true")
    print("  - USE_MOCK_SERVICES: true")


def run_with_pytest():
    import pytest

    test_dir = Path(__file__).parent
    pytest_args = [
        str(test_dir),
        "-v",
        "-s",
        "--tb=short",
        "-m",
        "e2e",
        "--log-cli-level=INFO",
    ]
    print(f"Running pytest with args: {' '.join(pytest_args)}")
    exit_code = pytest.main(pytest_args)
    return exit_code == 0


def run_standalone():
    print("🚀 Running E2E test as standalone script")
    # Use uv run to ensure correct environment
    import subprocess

    cmd = ["uv", "run", "python", str(Path(__file__).parent / "test_runner.py")]
    result = subprocess.run(cmd, cwd=str(project_root))
    return result.returncode == 0


def main():
    print("🎬 Video Grouper End-to-End Test Runner")
    setup_test_environment()
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "standalone"
    success = False
    if mode == "pytest":
        success = run_with_pytest()
    elif mode == "standalone":
        success = run_standalone()
    else:
        print(f"❌ Unknown mode: {mode}")
        return 1
    print("=" * 50)
    if success:
        print("🎉 E2E Test PASSED!")
        return 0
    else:
        print("❌ E2E Test FAILED!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
