"""Tests for the auto-upgrade JSON journal."""

import json
import logging

from video_grouper.update.journal import (
    UpdateJournalEntry,
    UpdateLoggerAdapter,
    append_entry,
    journal_path,
    new_update_id,
    read_latest_entries,
)


def test_new_update_id_is_8_hex_chars():
    uid = new_update_id()
    assert len(uid) == 8
    int(uid, 16)  # must parse as hex


def test_new_update_id_is_unique():
    assert new_update_id() != new_update_id()


def test_append_and_read_one_entry(tmp_path):
    entry = UpdateJournalEntry(
        id="abcd1234",
        started_at=1700000000.0,
        from_version="0.3.6",
        source_url="https://example.com/r/l",
        auto_update=True,
    )
    entry.finalize("installed", to_version="0.3.7", stages_completed=["check"])

    append_entry(tmp_path, entry)

    entries = read_latest_entries(tmp_path, limit=1)
    assert len(entries) == 1
    assert entries[0]["id"] == "abcd1234"
    assert entries[0]["from_version"] == "0.3.6"
    assert entries[0]["to_version"] == "0.3.7"
    assert entries[0]["outcome"] == "installed"
    assert entries[0]["duration_ms"] is not None


def test_append_creates_logs_directory(tmp_path):
    entry = UpdateJournalEntry(
        id="aa",
        started_at=0.0,
        from_version="0.0.1",
        source_url="x",
        auto_update=False,
    )
    append_entry(tmp_path, entry)
    assert journal_path(tmp_path).exists()


def test_read_latest_returns_empty_when_no_journal(tmp_path):
    assert read_latest_entries(tmp_path, limit=5) == []


def test_read_latest_returns_n_most_recent(tmp_path):
    for i in range(5):
        entry = UpdateJournalEntry(
            id=f"id{i:08x}"[:8],
            started_at=float(i),
            from_version="0.3.6",
            source_url="x",
            auto_update=True,
        )
        append_entry(tmp_path, entry)

    entries = read_latest_entries(tmp_path, limit=2)
    assert len(entries) == 2
    assert entries[0]["started_at"] == 3.0
    assert entries[1]["started_at"] == 4.0


def test_journal_entries_are_one_line_each(tmp_path):
    for i in range(3):
        entry = UpdateJournalEntry(
            id=f"id{i:08x}"[:8],
            started_at=float(i),
            from_version="0.3.6",
            source_url="x",
            auto_update=True,
        )
        append_entry(tmp_path, entry)

    with journal_path(tmp_path).open() as f:
        lines = [line for line in f if line.strip()]

    assert len(lines) == 3
    for line in lines:
        json.loads(line)


def test_update_logger_adapter_prefixes_messages(caplog):
    base = logging.getLogger("test_update_adapter")
    adapter = UpdateLoggerAdapter(base, {"update_id": "abcd1234"})

    with caplog.at_level(logging.INFO, logger="test_update_adapter"):
        adapter.info("downloading file")

    assert any(
        "[update:abcd1234] downloading file" in record.message
        for record in caplog.records
    )
