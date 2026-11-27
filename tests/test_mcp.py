"""Tests for DiploidMeshMcpServer mesh_send inference and thread recording."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from diploid_agent.config import MeshConfig
from mesh_core.delivery import DeliveryResult

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.history import history_path
from diploid_mesh.mcp import DiploidMeshMcpServer


def _server(tmp_path: Path, chat_id: str = "12345") -> DiploidMeshMcpServer:
    mesh_cfg = MeshConfig(
        agent_name="agent",
        private_key_path=tmp_path / "test.pem",
        vault_path=tmp_path / "vault",
        allow_loopback=True,
        chat_map={"chat": chat_id, "review": chat_id},
    )
    config = DiploidMeshConfig(mesh_cfg)
    args = argparse.Namespace(
        chat_id=chat_id,
        sessions_root=str(tmp_path / "sessions"),
        state_file="chat_mesh_state.json",
        harness_url="",
    )
    return DiploidMeshMcpServer(config, args)


def _write_state(server: DiploidMeshMcpServer, data: dict) -> None:
    server.tracker.state_path.parent.mkdir(parents=True, exist_ok=True)
    server.tracker.state_path.write_text(json.dumps(data, default=str))


def _read_state(server: DiploidMeshMcpServer) -> dict:
    if not server.tracker.state_path.exists():
        return {}
    return json.loads(server.tracker.state_path.read_text(encoding="utf-8"))


def test_infer_thread_from_current_mesh(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "current_mesh": {
                "sender": "peer",
                "session": "review",
                "from_session": "chat",
                "message_id": "msg-1",
                "reply": "yes",
            },
        },
    )
    thread = server._infer_thread("peer")
    assert thread == {"session": "chat", "from_session": "review", "ref": "msg-1"}


def test_infer_thread_from_inbound_thread(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "mesh_threads": {
                "peer": {
                    "sender": "peer",
                    "session": "chat",
                    "from_session": "review",
                    "message_id": "msg-2",
                    "direction": "inbound",
                }
            },
        },
    )
    thread = server._infer_thread("peer")
    assert thread == {"session": "review", "from_session": "chat", "ref": "msg-2"}


def test_infer_thread_from_outbound_thread(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "mesh_threads": {
                "peer": {
                    "sender": "agent",
                    "session": "chat",
                    "from_session": "review",
                    "message_id": "msg-3",
                    "ref": "msg-1",
                    "direction": "outbound",
                }
            },
        },
    )
    thread = server._infer_thread("peer")
    assert thread == {"session": "chat", "from_session": "review", "ref": "msg-3"}


def test_infer_thread_first_message_falls_back_to_chat_map(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(server, {})
    thread = server._infer_thread("new-peer")
    assert thread == {"session": "chat", "from_session": "chat", "ref": None}


def test_infer_thread_prefers_current_mesh_for_matching_recipient(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "current_mesh": {
                "sender": "peer",
                "session": "review",
                "from_session": "chat",
                "message_id": "active-1",
            },
            "mesh_threads": {
                "peer": {
                    "sender": "peer",
                    "session": "chat",
                    "from_session": "review",
                    "message_id": "stale-1",
                    "direction": "inbound",
                }
            },
        },
    )
    thread = server._infer_thread("peer")
    assert thread["ref"] == "active-1"
    assert thread == {"session": "chat", "from_session": "review", "ref": "active-1"}


def test_infer_thread_drops_ref_to_closed_thread(tmp_path: Path) -> None:
    """A ref to a closed thread is rejected as THREAD_CLOSED on receipt —
    _infer_thread must start a fresh thread instead of ref-ing it."""
    from mesh_core.threads import record as record_close

    server = _server(tmp_path)
    _write_state(
        server,
        {
            "mesh_threads": {
                "peer": {
                    "sender": "peer",
                    "session": "chat",
                    "from_session": "review",
                    "message_id": "closed-anchor-mcp-1",
                    "direction": "inbound",
                }
            },
        },
    )
    record_close(
        "closed-anchor-mcp-1",
        "peer",
        vault_path=server.mesh.core_config.vault_path,
    )
    thread = server._infer_thread("peer")
    assert thread == {"session": "review", "from_session": "chat", "ref": None}


def test_infer_thread_ignores_current_mesh_for_other_recipient(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "current_mesh": {
                "sender": "peer",
                "session": "review",
                "from_session": "chat",
                "message_id": "active-1",
            },
            "mesh_threads": {
                "peer2": {
                    "sender": "peer2",
                    "session": "review",
                    "from_session": "chat",
                    "message_id": "peer2-1",
                    "direction": "inbound",
                }
            },
        },
    )
    thread = server._infer_thread("peer2")
    assert thread == {"session": "chat", "from_session": "review", "ref": "peer2-1"}


def test_mesh_send_handler_uses_inferred_ref_and_records_thread(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "current_mesh": {
                "sender": "peer",
                "session": "review",
                "from_session": "chat",
                "message_id": "msg-1",
                "reply": "yes",
            },
        },
    )

    send_spy: dict[str, Any] = {}

    def fake_send(*, recipient, body, action, reply, ref, msg_id, session, from_session):
        send_spy["kwargs"] = {
            "recipient": recipient,
            "body": body,
            "action": action,
            "reply": reply,
            "ref": ref,
            "msg_id": msg_id,
            "session": session,
            "from_session": from_session,
        }
        return DeliveryResult(delivery_id="d-123")

    server.mesh.send = fake_send
    server.tracker.notify_telegram = MagicMock()

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "mesh_send",
            "arguments": {
                "agent": "peer",
                "message": "replying",
            },
        },
    }
    response = server._handle(request)

    assert response and not response["result"].get("isError")
    assert send_spy["kwargs"]["ref"] == "msg-1"
    assert send_spy["kwargs"]["session"] == "chat"
    assert send_spy["kwargs"]["from_session"] == "review"
    assert send_spy["kwargs"]["msg_id"]

    state = _read_state(server)
    assert state["mesh_threads"]["peer"]["direction"] == "outbound"
    assert state["mesh_threads"]["peer"]["ref"] == "msg-1"

    history_lines = history_path(tmp_path / "sessions", "12345").read_text().splitlines()
    assert len(history_lines) == 1
    event = json.loads(history_lines[0])
    assert event["direction"] == "outbound"
    assert event["delivery_state"] == "outbound-recipient-accepted"
    assert event["delivery_id"] == "d-123"
    assert event["ref"] == "msg-1"
    assert event["body"] == "replying"


def test_mesh_send_on_reply_end_turn_starts_new_thread(tmp_path: Path) -> None:
    """Regression: a turn woken by reply=end must not be blocked from
    mesh_send — the contract allows a genuinely new thread, and the closed
    thread's id is dropped from the inferred ref."""
    from mesh_core.threads import record as record_close

    server = _server(tmp_path)
    _write_state(
        server,
        {
            "current_mesh": {
                "sender": "peer",
                "session": "chat",
                "from_session": "chat",
                "message_id": "end-anchor-1",
                "reply": "end",
            },
        },
    )
    record_close(
        "end-anchor-1",
        "peer",
        vault_path=server.mesh.core_config.vault_path,
    )

    send_spy: dict[str, Any] = {}

    def fake_send(*, recipient, body, action, reply, ref, msg_id, session, from_session):
        send_spy["kwargs"] = {"recipient": recipient, "ref": ref, "msg_id": msg_id}
        return DeliveryResult(delivery_id="d-456")

    server.mesh.send = fake_send
    server.tracker.notify_telegram = MagicMock()

    response = server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mesh_send",
                "arguments": {"agent": "peer", "message": "new topic"},
            },
        }
    )

    assert response and not response["result"].get("isError")
    assert send_spy["kwargs"]["ref"] is None  # new thread, not a closed-thread ref


def test_mesh_send_explicit_ref_passes_through(tmp_path: Path) -> None:
    """An explicit ref is threaded as given — the receiver's ingress owns
    THREAD_CLOSED rejection; the send gate does not pre-empt it."""
    server = _server(tmp_path)
    _write_state(server, {})

    send_spy: dict[str, Any] = {}

    def fake_send(*, recipient, body, action, reply, ref, msg_id, session, from_session):
        send_spy["kwargs"] = {"ref": ref}
        return DeliveryResult(delivery_id="d-789")

    server.mesh.send = fake_send
    server.tracker.notify_telegram = MagicMock()

    response = server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mesh_send",
                "arguments": {
                    "agent": "peer",
                    "message": "explicit",
                    "ref": "some-msg-id",
                },
            },
        }
    )

    assert response and not response["result"].get("isError")
    assert send_spy["kwargs"]["ref"] == "some-msg-id"


def test_mesh_send_journals_refused_and_failed_attempts(tmp_path: Path) -> None:
    server = _server(tmp_path)

    refused = SimpleNamespace(
        delivery_id=None,
        error="busy",
        detail={"reason": "busy", "thread_id": "lease-9", "retry_after": 42},
        journal_delivery_state="outbound-recipient-refused",
        claim_thread_id="lease-9",
    )
    server.mesh.send = MagicMock(return_value=refused)
    server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mesh_send",
                "arguments": {"agent": "peer", "message": "refused attempt", "reply": "yes"},
            },
        }
    )
    event = json.loads(history_path(tmp_path / "sessions", "12345").read_text().splitlines()[0])
    assert event["delivery_state"] == "outbound-recipient-refused"
    assert event["delivery_error"] == "busy"
    assert event["lease_thread_id"] == "lease-9"
    assert event["delivery_detail"]["thread_id"] == "lease-9"

    failed = SimpleNamespace(
        delivery_id=None,
        error="unreachable",
        detail=None,
        journal_delivery_state="outbound-send-failed",
    )
    server.mesh.send = MagicMock(return_value=failed)
    server._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "mesh_send",
                "arguments": {"agent": "peer", "message": "failed attempt", "reply": "yes"},
            },
        }
    )
    events = [
        json.loads(line)
        for line in history_path(tmp_path / "sessions", "12345").read_text().splitlines()
    ]
    assert events[1]["delivery_state"] == "outbound-send-failed"
    assert events[1]["delivery_error"] == "unreachable"
    assert events[1]["body"] == "failed attempt"

    server.mesh.send = MagicMock(return_value=DeliveryResult(error="busy"))
    server._handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "mesh_send",
                "arguments": {"agent": "peer", "message": "bare reason", "reply": "yes"},
            },
        }
    )
    events = [
        json.loads(line)
        for line in history_path(tmp_path / "sessions", "12345").read_text().splitlines()
    ]
    assert events[2]["delivery_state"] == "outbound-send-failed"


def test_mesh_thread_tool_is_listed(tmp_path: Path) -> None:
    server = _server(tmp_path)
    response = server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    tools = response["result"]["tools"]
    thread_tool = next(tool for tool in tools if tool["name"] == "mesh_thread")
    assert "ref" in thread_tool["inputSchema"]["required"]
    assert thread_tool["inputSchema"]["properties"]["limit"]["default"] == 20
    assert thread_tool["inputSchema"]["properties"]["max_chars"]["default"] == 12000


def test_mesh_thread_tool_returns_newest_first_full_body_json(tmp_path: Path) -> None:
    from diploid_mesh.history import append_event, make_event

    server = _server(tmp_path)
    for index in (1, 2):
        event = make_event(
            kind="message",
            msg_id=f"tool-{index}",
            ref="tool-slug",
            lease_thread_id=None,
            wire_thread=None,
            sender="agent",
            recipient="peer",
            direction="outbound",
            action="info",
            reply="yes",
            session="chat",
            from_session="chat",
            body=f"full body {index}",
            chat_id="12345",
            event_ts=float(index),
            delivery_state="outbound-recipient-accepted",
        )
        append_event(tmp_path / "sessions", "12345", event)

    response = server._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "mesh_thread", "arguments": {"ref": "tool-slug", "limit": 1}},
        }
    )

    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert [event["msg_id"] for event in payload["events"]] == ["tool-2"]
    assert payload["events"][0]["body"] == "full body 2"
    assert payload["events"][0]["resolution"] == "exact-slug"
    assert payload["page"]["elided_entries"] == 1
    assert payload["page"]["next_before"]


def test_mesh_thread_tool_surfaces_invalid_cursor_as_tool_error(tmp_path: Path) -> None:
    server = _server(tmp_path)
    response = server._handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "mesh_thread",
                "arguments": {"ref": "tool-slug", "before": "bad-cursor"},
            },
        }
    )

    assert response["result"]["isError"] is True
    assert "before cursor" in response["result"]["content"][0]["text"]
