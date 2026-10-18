import logging
from datetime import UTC, datetime
from uuid import uuid4

from sidewit.generation.history import GenerationHistory, GenerationRecord


def record(context=None):
    return GenerationRecord(
        context_id=context or uuid4(),
        context_version=1,
        question="PRIVATE_QUESTION",
        reference_time=datetime.now(UTC),
        provider="fake",
        model="fixture",
        prompt_version="test",
        operation={"statement": "SELECT PRIVATE_SQL", "parameters": {"value": "PRIVATE_VALUE"}},
    )


def test_bounded_history_and_context_deletion(caplog):
    history = GenerationHistory(2)
    shared = uuid4()
    first, second, third = record(shared), record(), record(shared)
    with caplog.at_level(logging.INFO):
        for item in (first, second, third):
            history.add(item)
    assert history.get(first.id) is None
    assert history.recent(None, 2) == [third, second]
    assert history.recent(shared, 2) == [third]
    history.forget_context(shared)
    assert history.recent(None, 2) == [second]
    # Keeping/evicting in-memory content does not emit a second operational event.
    assert not caplog.records
    for private in ("PRIVATE_QUESTION", "PRIVATE_SQL", "PRIVATE_VALUE"):
        assert private not in caplog.text


def test_new_process_history_has_no_old_records():
    original = GenerationHistory(2)
    item = record()
    original.add(item)
    replacement = GenerationHistory(2)
    assert replacement.get(item.id) is None
