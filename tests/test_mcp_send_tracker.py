"""Tests for MeshSendTracker nudge + cap behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import respx
from httpx import Response

from diploid_mesh.mcp import MeshSendTracker


def _tracker(
    harness_url: str = "http://127.0.0.1:4003",
    max_sends: int = 3,
    suggestion: int = 2,
    state_path: Path | None = None,
) -> MeshSendTracker:
    return MeshSendTracker(
        "mesh:test",
        harness_url,
        state_path or Path("/tmp/fake_mesh_state.json"),
        None,
        max_sends,
        suggestion,
    )


@respx.mock
def test_tracker_allows_until_cap_and_nudges() -> None:
    state = Path("/tmp/test_mesh_state_yes.json")
    state.write_text(json.dumps({"current_mesh": {"reply": "yes"}}))
    route = respx.get("http://127.0.0.1:4003/turn/mesh:test").mock(
        return_value=Response(
            200,
            json={
                "chat_id": "mesh:test",
                "status": "running",
                "start_time": 100.0,
            },
        )
    )
    tracker = _tracker(max_sends=3, suggestion=2, state_path=state)
    assert tracker.allowed() == (True, None)
    assert tracker.allowed() == (
        True,
        (
            "You have sent 2 mesh message(s) this turn. "
            "If you are done, use reply=end on the next send to close the thread."
        ),
    )
    assert tracker.allowed() == (
        True,
        (
            "This is the last mesh_send allowed this turn. "
            "If you are done, use reply=end to close the thread."
        ),
    )
    assert tracker.allowed() == (False, "Mesh send budget (3) exceeded for this turn.")
    assert route.called


@respx.mock
def test_tracker_blocks_reply_end() -> None:
    state = Path("/tmp/test_mesh_state_end.json")
    state.write_text(json.dumps({"current_mesh": {"reply": "end"}}))
    route = respx.get("http://127.0.0.1:4003/turn/mesh:test").mock(
        return_value=Response(
            200,
            json={
                "chat_id": "mesh:test",
                "status": "running",
                "start_time": 100.0,
            },
        )
    )
    tracker = _tracker(state_path=state)
    allowed, err = tracker.allowed()
    assert allowed is False
    assert err and "reply=end" in err
    assert not route.called


@respx.mock
def test_tracker_allows_reply_no_until_cap(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"current_mesh": {"reply": "no"}}))
    respx.get("http://127.0.0.1:4003/turn/mesh:test").mock(
        return_value=Response(
            200,
            json={
                "chat_id": "mesh:test",
                "status": "running",
                "start_time": 100.0,
            },
        )
    )
    tracker = _tracker(max_sends=3, suggestion=2, state_path=state)
    assert tracker.allowed()[0] is True
    hint = tracker.allowed()[1]
    assert hint and "reply=end" in hint
