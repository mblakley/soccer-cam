"""Tasks for TTT-cloud feature queues.

ClipRequestTask lives in ``tasks/clip/`` for historical reasons; the
three new TTT-feature tasks live here. Each carries the raw TTT API
response dict in ``payload`` plus a stable ``ttt_id`` for queue dedup.
"""
