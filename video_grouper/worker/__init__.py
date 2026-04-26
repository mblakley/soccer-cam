"""Remote-worker mode for soccer-cam.

A worker node does NOT run the orchestrator (camera poll, download,
state management). It polls a master's ``/api/work/next`` for tasks
matching its capabilities, runs them, and reports back. State stays
with the master; the worker just executes.

Run with::

    python -m video_grouper.worker [--master http://master.local:8765]
"""
