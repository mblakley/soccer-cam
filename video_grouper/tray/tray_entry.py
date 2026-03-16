"""Entry point for PyInstaller-built tray executable."""

import asyncio
from video_grouper.task_processors.register_tasks import register_all_tasks
from video_grouper.tray.main import main

if __name__ == "__main__":
    register_all_tasks()
    asyncio.run(main())
