"""NullWorkSource (TTT off): never any work, nothing claimable, every report a
no-op."""

from types import SimpleNamespace

import pytest

from video_grouper.integrations.registry import IntegrationContext
from video_grouper.integrations.work import KINDS, WorkItem
from video_grouper.integrations.work.sources.null import NullWorkSource


def test_always_available():
    ctx = IntegrationContext(SimpleNamespace(ttt=SimpleNamespace(enabled=False)), ".")
    assert isinstance(NullWorkSource.from_context(ctx), NullWorkSource)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_no_work_and_every_verb_is_a_no_op(kind):
    source = NullWorkSource()
    item = WorkItem(kind, "w-1", {"id": "w-1", "recording_id": "rec-1"})

    assert await source.pending() == []
    assert await source.pending([kind]) == []
    assert await source.claim(item) is None
    assert await source.details(item) == {}
    assert await source.latest(item) == {}
    assert await source.progress(item, "step", 50, {"message": "m"}) is None
    assert await source.blocked(item, "reason") is None
    assert await source.complete(item, {"url": "u"}) is None
    assert await source.fail(item, "error") is None
    assert await source.cancelled(item) is None
