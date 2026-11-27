from __future__ import annotations

import json
from pathlib import Path

import pytest

from diploid_mesh.history import (
    append_event,
    count_exact_related_events,
    history_path,
    iter_events,
    make_event,
    validate_event,
)


def test_schema_v1_fixtures_are_valid() -> None:
    root = Path(__file__).parent / "fixtures" / "mesh-history-schema-v1" / "sessions"
    paths = sorted(root.glob("*/chat_mesh_history.jsonl"))
    assert len(paths) == 2
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            validate_event(json.loads(line))


def test_append_event_writes_valid_jsonl_and_counts_across_sessions(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    first = make_event(
        kind="message",
        msg_id="m1",
        ref="thread-key",
        lease_thread_id=None,
        wire_thread=None,
        sender="alpha",
        recipient="beta",
        direction="outbound",
        action="do",
        reply="yes",
        session="chat",
        from_session="chat",
        body="first",
        chat_id="chat-a",
        event_ts=1.0,
        delivery_state="outbound-recipient-accepted",
    )
    second = make_event(
        kind="message",
        msg_id="m2",
        ref="thread-key",
        lease_thread_id=None,
        wire_thread=None,
        sender="beta",
        recipient="alpha",
        direction="inbound",
        action="info",
        reply="yes",
        session="review",
        from_session="review",
        body="second",
        chat_id="review-a",
        event_ts=2.0,
        delivery_state="inbound-wake-enqueued",
    )
    append_event(sessions, "chat-a", first)
    append_event(sessions, "review-a", second)

    lines = history_path(sessions, "chat-a").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    validate_event(json.loads(lines[0]))
    assert count_exact_related_events(sessions, "thread-key", exclude_msg_id="m2") == 1


def test_validate_event_rejects_direction_delivery_mismatch(tmp_path: Path) -> None:
    event = make_event(
        kind="message",
        msg_id="m1",
        ref=None,
        lease_thread_id=None,
        wire_thread=None,
        sender="alpha",
        recipient="beta",
        direction="outbound",
        action="do",
        reply="yes",
        session=None,
        from_session=None,
        body="x",
        chat_id="chat",
        event_ts=1.0,
        delivery_state="inbound-wake-enqueued",
    )
    with pytest.raises(ValueError, match="outbound delivery_state"):
        validate_event(event)


def test_fixture_cross_session_slug_and_failure_coverage() -> None:
    sessions = Path(__file__).parent / "fixtures" / "mesh-history-schema-v1" / "sessions"
    assert count_exact_related_events(sessions, "cross-session-slug") == 2
    assert count_exact_related_events(sessions, "design-slug") == 2
    events = list(iter_events(sessions))
    by_id = {event["msg_id"]: event for event in events}
    assert by_id["failed-1"]["delivery_state"] == "outbound-send-failed"
    assert by_id["local-refused-1"]["delivery_state"] == "outbound-local-refused"
    assert by_id["digest-1"]["delivery_state"] == "inbound-digest-queued"


def test_validate_event_rejects_boolean_schema_version(tmp_path: Path) -> None:
    event = make_event(
        kind="message",
        msg_id="m1",
        ref=None,
        lease_thread_id=None,
        wire_thread=None,
        sender="alpha",
        recipient="beta",
        direction="outbound",
        action="do",
        reply="yes",
        session=None,
        from_session=None,
        body="x",
        chat_id="chat",
        event_ts=1.0,
        delivery_state="outbound-recipient-accepted",
    )
    event["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        validate_event(event)
