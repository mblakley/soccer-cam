"""TTTCameraIds: TTT's ids for this install's cameras, shared by the TTT sources."""

from video_grouper.integrations.ttt_cameras import TTTCameraIds


def test_registered_ids_survive_a_restart(tmp_path):
    TTTCameraIds(str(tmp_path)).update({"front": "c-1", "back": "c-2"})
    again = TTTCameraIds(str(tmp_path))
    assert again.id_for("front") == "c-1"
    assert again.all_ids() == {"front": "c-1", "back": "c-2"}


def test_the_configured_id_is_the_fallback(tmp_path):
    ids = TTTCameraIds(str(tmp_path), "typed")
    assert ids.id_for("front") == "typed"
    assert ids.all_ids() == {"": "typed"}
    ids.update({"front": "c-1"})
    assert ids.id_for("front") == "c-1"
    assert ids.all_ids() == {"front": "c-1"}  # registered ids replace it


def test_no_camera_name_means_the_only_camera(tmp_path):
    ids = TTTCameraIds(str(tmp_path))
    ids.update({"front": "c-1"})
    assert ids.id_for(None) == "c-1"
    ids.update({"back": "c-2"})
    assert ids.id_for(None) is None  # ambiguous


def test_in_memory_store_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ids = TTTCameraIds(None)
    ids.update({"front": "c-1"})
    assert ids.id_for("front") == "c-1"
    assert list(tmp_path.iterdir()) == []


def test_a_named_camera_ttt_did_not_register_has_no_id(tmp_path):
    """Once any camera has registered, an unregistered named camera (TTT
    refused it) gets None -- not the configured id, which would report camera
    B's recordings under camera A."""
    ids = TTTCameraIds(str(tmp_path), "typed")
    assert ids.id_for("back") == "typed"  # nothing registered yet: fallback
    ids.update({"front": "c-1"})
    assert ids.id_for("front") == "c-1"
    assert ids.id_for("back") is None
    assert ids.id_for(None) == "c-1"  # unnamed: the only registered camera
    ids.update({"side": "c-3"})
    assert ids.id_for(None) == "typed"  # unnamed and ambiguous: configured id
