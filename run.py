#!/usr/bin/env python
"""
Simple wrapper script to run the video_grouper application.
"""

import os
import sys
import asyncio

# Add the current directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the main function from video_grouper
from video_grouper.__main__ import main

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
