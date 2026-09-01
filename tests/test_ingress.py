"""Tests for diploid-mesh ingress handler."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from diploid_agent.config import (
    Config,
    DiploidConfig,
    HarnessConfig,
    MeshConfig,
    PersonaConfig,
    PlanConfig,
    PluginConfig,
    Secrets,
    TimerConfig,
)
from diploid_agent.plugins.contexts import PromptContext
from diploid_agent.runtime.agent_runtime import AgentRuntime
from diploid_agent.transport.http import create_app
from fastapi.testclient import TestClient
from mesh_core import MeshEnvelope, generate_keypair, sign_message

from diploid_mesh.mcp import MeshSendTracker
from diploid_mesh.plugin import DiploidMeshPlugin


class FakeEngine:
    def prompt(self, *a, **k):
        from diploid_agent.engine import TurnResult

        return TurnResult(reply="ok", session_id="s1")

    def list_models(self):
        return ["m1"]

    def restart(self):
        pass

    def is_stale_session_error(self, exc):
        return False

    def close(self):
        pass


def _make_config(tmp_path: Path) -> Config:
    return Config(
        diploid=DiploidConfig(bin="/bin/echo", model="swe-1-7"),
        persona=PersonaConfig(
            name="test-pilot",
            profile_root=Path(__file__).parent / "fixtures" / "test-pilot",
        ),
        harness=HarnessConfig(
            sessions_root=tmp_path / "sessions",
            session_store_path=tmp_path / "sessions.jsonl",
            plan=PlanConfig(root=tmp_path / "plans"),
            memory={"backend": "file"},  # type: ignore[arg-type]
            timer=TimerConfig(enabled=False, interval_seconds=0.1),
            mesh=MeshConfig(
                enabled=True,
                agent_name="diploid-0",
                ingress_module="diploid_mesh.ingress",
                chat_mapping="per_sender",
                fallback_chat_id="mesh:inbox",
            ),
        ),
        secrets=Secrets(WINDSURF_API_KEY="test-key"),
    )


def _sign_and_send(
    client: TestClient,
    private_pem: str,
    sender: str,
    recipient: str,
    body: str,
    *,
    public_pem: str,
    sender_identity: dict,
    vault_path: Path,
    reply: str = "yes",
    is_dsn: bool = False,
    msg_id: str = "msg-1",
) -> None:
    # Write sender identity into the vault so the receiver can verify.
    sender_dir = vault_path / "mesh" / "agents" / sender
    sender_dir.mkdir(parents=True, exist_ok=True)
    sender_identity["name"] = sender
    sender_identity["transports"] = {
        "hermes_webhook": {
            "protocol": "hermes-webhook",
            "url": "http://127.0.0.1:4003/mesh/receive",
            "auth": {"public_key": public_pem},
        }
    }
    from mesh_core.identity import IdentityVault, MeshIdentity

    vault = IdentityVault(root=vault_path)
    vault.save(
        sender,
        MeshIdentity(
            id=sender,
            name=sender,
            role="agent",
            description="test",
            url="",
            public_key=public_pem,
        ),
    )

    envelope = MeshEnvelope(
        sender=sender,
        recipient=recipient,
        msg_id=msg_id,
        action="do",
        reply=reply,
        body=body,
    )
    text = envelope.build()
    ts = str(time.time())
    body_json = json.dumps({"from": sender, "text": text}, sort_keys=True)
    signed = f"{ts}\n{body_json}".encode()
    signature = sign_message(private_pem, signed)

    headers = {
        "X-Mesh-Timestamp": ts,
        "X-Mesh-Signature": signature,
        "Content-Type": "application/octet-stream",
    }
    if is_dsn:
        headers["X-Mesh-Dsn"] = "1"

    return client.post(
        "/mesh/receive",
        data=body_json.encode(),
        headers=headers,
    )


@pytest.fixture
def client(tmp_path: Path):
    config = _make_config(tmp_path)
    # Set vault path in config to temp.
    config.harness.mesh.vault_path = tmp_path / "mesh-vault"
    config.harness.mesh.private_key_path = tmp_path / "diploid-0.pem"
    runtime = AgentRuntime(config)
    runtime.engine = FakeEngine()
    with TestClient(create_app(config, runtime)) as client:
        yield client


@pytest.fixture
def client_runtime(tmp_path: Path):
    config = _make_config(tmp_path)
    config.harness.mesh.vault_path = tmp_path / "mesh-vault"
    config.harness.mesh.private_key_path = tmp_path / "diploid-0.pem"
    runtime = AgentRuntime(config)
    runtime.engine = FakeEngine()
    with TestClient(create_app(config, runtime)) as client:
        yield client, runtime


def test_mesh_receive_triggers_wake(tmp_path: Path, client: TestClient) -> None:
    private, public = generate_keypair()
    resp = _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "Hello from Hermes",
        public_pem=public,
        sender_identity={},
        vault_path=tmp_path / "mesh-vault",
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["chat_id"] == "mesh:hermes-0"


def test_mesh_receive_reply_classifications(
    tmp_path: Path, client_runtime: tuple[TestClient, AgentRuntime], monkeypatch
) -> None:
    client, runtime = client_runtime
    private, public = generate_keypair()
    vault_path = tmp_path / "mesh-vault"

    spy = {"record": None}

    def record_mock(*a, **k):
        spy["record"] = (a, k)

    monkeypatch.setattr(runtime, "record_mesh_message", record_mock)

    for reply, msg_id in [("yes", "msg-yes"), ("no", "msg-no"), ("end", "msg-end")]:
        resp = _sign_and_send(
            client,
            private,
            "hermes-0",
            "diploid-0",
            f"Hello-{reply}",
            public_pem=public,
            sender_identity={},
            vault_path=vault_path,
            reply=reply,
            msg_id=msg_id,
        )
        assert resp.status_code == 202
        assert resp.json()["chat_id"] == "mesh:hermes-0"
        assert spy["record"] is None
        # The waker is disabled in this test, so the wake stays in the queue
        # and the handler returns without running a turn.
        event = runtime.wake_queue.get(f"mesh:{msg_id}")
        assert event is not None, f"wake not enqueued for reply={reply}"
        assert event.reason == "mesh"
        assert event.payload["mesh"]["reply"] == reply
        assert event.payload.get("silent") is False
        spy["record"] = None

    # DSN is terminal: record only, no wake.
    for msg_id in ("msg-dsn", "msg-dsn-header"):
        resp = _sign_and_send(
            client,
            private,
            "hermes-0",
            "diploid-0",
            "[mesh-dsn] delivered" if msg_id == "msg-dsn" else "delivered",
            public_pem=public,
            sender_identity={},
            vault_path=vault_path,
            is_dsn=True,
            msg_id=msg_id,
        )
        assert resp.status_code == 202
        assert spy["record"] is not None
        assert runtime.wake_queue.get(f"mesh:{msg_id}") is None
        spy["record"] = None


def _make_mesh_plugin(runtime: AgentRuntime, chat_id: str) -> DiploidMeshPlugin:
    config = PluginConfig(
        name="mesh",
        module="diploid_mesh",
        prompt_slot="mesh",
        state_file="chat_mesh_state.json",
        enabled=True,
    )
    return DiploidMeshPlugin(
        config, chat_id, runtime.sessions_root, runtime=runtime
    )


def test_mesh_prompt_after_prompt_built_adds_reply_cta(
    client_runtime: tuple[TestClient, AgentRuntime],
) -> None:
    _client, runtime = client_runtime
    mesh_plugin = _make_mesh_plugin(runtime, "mesh:hermes-0")

    mesh_plugin._state["current_mesh"] = {
        "sender": "hermes-0",
        "body": "ping",
        "reply": "yes",
    }
    pctx = PromptContext(
        prompt="## Identity\n\n## User\nping",
        notice=None,
        memory_flags={},
        slots={},
    )
    result = mesh_plugin.after_prompt_built(pctx)
    assert result is not None
    assert "# SYSTEM — MESH REPLY RULE" in result.prompt
    assert "You MUST use the `mesh_send` tool" in result.prompt
    assert "Do NOT put the mesh payload in your final assistant text" in result.prompt


def test_mesh_prompt_after_prompt_built_adds_silence_cta(
    client_runtime: tuple[TestClient, AgentRuntime],
) -> None:
    _client, runtime = client_runtime
    mesh_plugin = _make_mesh_plugin(runtime, "mesh:hermes-0")

    mesh_plugin._state["current_mesh"] = {
        "sender": "hermes-0",
        "body": "done",
        "reply": "end",
    }
    pctx = PromptContext(
        prompt="## Identity\n\n## User\ndone",
        notice=None,
        memory_flags={},
        slots={},
    )
    result = mesh_plugin.after_prompt_built(pctx)
    assert result is not None
    assert "# SYSTEM — MESH SILENCE RULE" in result.prompt


def test_mesh_send_tracker_floats_to_telegram(
    tmp_path: Path, monkeypatch
) -> None:
    tracker = MeshSendTracker(
        chat_id="7945905361",
        harness_url="http://127.0.0.1:4003",
        state_path=tmp_path / "state.json",
        api_key="secret",
        max_sends=3,
        max_message_in_turn_suggestion=2,
    )
    mock_client = MagicMock()
    tracker._client = mock_client

    tracker.notify_telegram(
        sender="vesper",
        recipient="aurelia",
        body="pong",
        action="info",
        reply="end",
        msg_id="msg-123",
    )

    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "/mesh/7945905361/notify"
    assert call_args.kwargs["json"] == {
        "sender": "vesper",
        "recipient": "aurelia",
        "body": "pong",
        "action": "info",
        "reply": "end",
        "msg_id": "msg-123",
    }
    assert call_args.kwargs["headers"]["X-API-Key"] == "secret"
