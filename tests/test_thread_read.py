"""Lane B read-side resolution and paging tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from diploid_mesh.history import append_event, make_event
from diploid_mesh.thread_read import read_thread_page, resolve_thread

FIXTURE_SESSIONS = Path("tests/fixtures/mesh-history-schema-v1/sessions")


def _write_event(
    root: Path,
    chat_id: str,
    msg_id: str,
    *,
    body: str,
    event_ts: float,
    ref: str | None = None,
) -> None:
    event = make_event(
        kind="message",
        msg_id=msg_id,
        ref=ref,
        lease_thread_id=None,
        wire_thread=None,
        sender="alpha",
        recipient="beta",
        direction="outbound",
        action="info",
        reply="yes",
        session="chat",
        from_session="chat",
        body=body,
        chat_id=chat_id,
        event_ts=event_ts,
        delivery_state="outbound-recipient-accepted",
    )
    append_event(root, chat_id, event)


def test_slug_resolution_matches_fixture_intent_without_fragmented_root() -> None:
    page = read_thread_page(FIXTURE_SESSIONS, "design-slug")

    assert [event["msg_id"] for event in page["events"]] == ["reply-2", "reply-1"]
    assert [event["resolution"] for event in page["events"]] == ["exact-slug", "exact-slug"]
    assert "root-1" not in {event["msg_id"] for event in page["events"]}
    assert page["resolution"]["resolved_entries"] == 2
    assert page["resolution"]["truncated"] is False


def test_cross_session_slug_resolution_reads_all_journals() -> None:
    page = read_thread_page(FIXTURE_SESSIONS, "cross-session-slug")

    assert [(event["msg_id"], event["chat_id"]) for event in page["events"]] == [
        ("cross-2", "review-a"),
        ("cross-1", "chat-a"),
    ]
    assert all(event["resolution"] == "exact-slug" for event in page["events"])


def test_uuid_child_resolution_uses_exact_ids_and_inferred_parent() -> None:
    page = read_thread_page(FIXTURE_SESSIONS, "uuid-child")

    assert [(event["msg_id"], event["resolution"]) for event in page["events"]] == [
        ("failed-1", "exact-id"),
        ("uuid-child", "exact-id"),
        ("uuid-root", "inferred-parent"),
    ]
    failed = page["events"][0]
    assert failed["delivery_state"] == "outbound-send-failed"
    assert failed["body"] == "This attempted reply did not observe acceptance."


def test_uuid_root_resolution_follows_connected_parent_edges_downward() -> None:
    page = read_thread_page(FIXTURE_SESSIONS, "uuid-root")

    assert [event["msg_id"] for event in page["events"]] == [
        "failed-1",
        "uuid-child",
        "uuid-root",
    ]
    assert page["events"][-1]["resolution"] == "exact-id"


def test_missing_key_returns_honest_empty_page() -> None:
    page = read_thread_page(FIXTURE_SESSIONS, "missing-key")

    assert page["events"] == []
    assert page["resolution"]["seed_matches"] == 0
    assert page["resolution"]["resolved_entries"] == 0
    assert page["page"]["has_older"] is False


def test_newest_first_limit_and_cursor_page_older_entries(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    for index in range(1, 6):
        _write_event(
            root,
            "chat-x",
            f"page-{index}",
            body=f"body-{index}",
            event_ts=float(index),
            ref="page-slug",
        )

    first = read_thread_page(root, "page-slug", limit=2, max_chars=1000)
    assert [event["msg_id"] for event in first["events"]] == ["page-5", "page-4"]
    assert first["page"]["elided_entries"] == 3
    assert first["page"]["has_older"] is True
    assert first["page"]["next_before"]

    second = read_thread_page(
        root, "page-slug", limit=2, max_chars=1000, before=first["page"]["next_before"]
    )
    assert [event["msg_id"] for event in second["events"]] == ["page-3", "page-2"]
    assert second["page"]["newer_entries"] == 2
    assert second["page"]["elided_entries"] == 1

    third = read_thread_page(
        root, "page-slug", limit=2, max_chars=1000, before=second["page"]["next_before"]
    )
    assert [event["msg_id"] for event in third["events"]] == ["page-1"]
    assert third["page"]["has_older"] is False
    assert third["page"]["next_before"] is None


def test_offset_pages_without_cursor(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    for index in range(1, 4):
        _write_event(
            root,
            "chat-x",
            f"offset-{index}",
            body=f"body-{index}",
            event_ts=float(index),
            ref="offset-slug",
        )

    page = read_thread_page(root, "offset-slug", limit=1, offset=1)
    assert [event["msg_id"] for event in page["events"]] == ["offset-2"]
    assert page["page"]["newer_entries"] == 1


def test_max_chars_selects_newest_full_bodies_and_reports_elision(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    for index in range(1, 4):
        _write_event(
            root,
            "chat-x",
            f"budget-{index}",
            body="x" * 8,
            event_ts=float(index),
            ref="budget-slug",
        )

    page = read_thread_page(root, "budget-slug", limit=10, max_chars=17)

    assert [event["msg_id"] for event in page["events"]] == ["budget-3", "budget-2"]
    assert all(len(event["body"]) == 8 for event in page["events"])
    assert page["page"]["returned_chars"] == 16
    assert page["page"]["elided_entries"] == 1
    assert page["page"]["elided_chars"] == 8
    assert page["page"]["single_event_exceeds_budget"] is False


def test_single_oversize_body_is_returned_whole_once(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_event(
        root,
        "chat-x",
        "oversize-1",
        body="x" * 50,
        event_ts=1.0,
        ref="oversize-slug",
    )

    page = read_thread_page(root, "oversize-slug", max_chars=10)

    assert len(page["events"]) == 1
    assert len(page["events"][0]["body"]) == 50
    assert page["page"]["single_event_exceeds_budget"] is True


def test_resolution_cap_is_explicitly_truncated(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    for index in range(1, 6):
        _write_event(
            root,
            "chat-x",
            f"chain-{index}",
            body=f"body-{index}",
            event_ts=float(index),
            ref=None if index == 1 else f"chain-{index - 1}",
        )

    resolved = resolve_thread(root, "chain-1", max_events=3)

    assert [event["msg_id"] for event in resolved["events"]] == ["chain-1", "chain-2", "chain-3"]
    assert resolved["truncated"] is True
    assert resolved["max_events"] == 3


def test_invalid_before_cursor_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="before cursor"):
        read_thread_page(tmp_path / "sessions", "anything", before="not-a-cursor")


def test_reader_ignores_additive_unknown_fields(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    _write_event(
        root,
        "chat-x",
        "additive-1",
        body="body",
        event_ts=1.0,
        ref="additive-slug",
    )
    path = root / "chat-x" / "chat_mesh_history.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    import json

    event = json.loads(lines[0])
    event["future_field"] = {"nested": True}
    path.write_text(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    page = read_thread_page(root, "additive-slug")

    assert [entry["msg_id"] for entry in page["events"]] == ["additive-1"]
    assert page["resolution"]["truncated"] is False
