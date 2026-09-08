"""Tests for DiploidMeshMcpServer mesh_send inference and thread recording."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from diploid_agent.config import MeshConfig
from mesh_core.delivery import DeliveryResult

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.mcp import DiploidMeshMcpServer


def _server(tmp_path: Path, chat_id: str = "12345") -> DiploidMeshMcpServer:
    mesh_cfg = MeshConfig(
        agent_name="aurelia",
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
                "sender": "vesper",
                "session": "review",
                "from_session": "chat",
                "message_id": "msg-1",
                "reply": "yes",
            },
        },
    )
    thread = server._infer_thread("vesper")
    assert thread == {"session": "chat", "from_session": "review", "ref": "msg-1"}


def test_infer_thread_from_inbound_thread(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "mesh_threads": {
                "vesper": {
                    "sender": "vesper",
                    "session": "chat",
                    "from_session": "review",
                    "message_id": "msg-2",
                    "direction": "inbound",
                }
            },
        },
    )
    thread = server._infer_thread("vesper")
    assert thread == {"session": "review", "from_session": "chat", "ref": "msg-2"}


def test_infer_thread_from_outbound_thread(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "mesh_threads": {
                "vesper": {
                    "sender": "aurelia",
                    "session": "chat",
                    "from_session": "review",
                    "message_id": "msg-3",
                    "ref": "msg-1",
                    "direction": "outbound",
                }
            },
        },
    )
    thread = server._infer_thread("vesper")
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
                "sender": "vesper",
                "session": "review",
                "from_session": "chat",
                "message_id": "active-1",
            },
            "mesh_threads": {
                "vesper": {
                    "sender": "vesper",
                    "session": "chat",
                    "from_session": "review",
                    "message_id": "stale-1",
                    "direction": "inbound",
                }
            },
        },
    )
    thread = server._infer_thread("vesper")
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
                "vesper": {
                    "sender": "vesper",
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
        "vesper",
        vault_path=server.mesh.core_config.vault_path,
    )
    thread = server._infer_thread("vesper")
    assert thread == {"session": "review", "from_session": "chat", "ref": None}


def test_infer_thread_ignores_current_mesh_for_other_recipient(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "current_mesh": {
                "sender": "vesper",
                "session": "review",
                "from_session": "chat",
                "message_id": "active-1",
            },
            "mesh_threads": {
                "vera": {
                    "sender": "vera",
                    "session": "review",
                    "from_session": "chat",
                    "message_id": "vera-1",
                    "direction": "inbound",
                }
            },
        },
    )
    thread = server._infer_thread("vera")
    assert thread == {"session": "chat", "from_session": "review", "ref": "vera-1"}


def test_mesh_send_handler_uses_inferred_ref_and_records_thread(tmp_path: Path) -> None:
    server = _server(tmp_path)
    _write_state(
        server,
        {
            "current_mesh": {
                "sender": "vesper",
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
                "agent": "vesper",
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
    assert state["mesh_threads"]["vesper"]["direction"] == "outbound"
    assert state["mesh_threads"]["vesper"]["ref"] == "msg-1"
    assert state["mesh_threads"]["vesper"]["message_id"] == send_spy["kwargs"]["msg_id"]
