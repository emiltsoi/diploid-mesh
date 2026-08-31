"""Tests for diploid-mesh ingress handler."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from diploid_agent.config import (
    Config,
    DiploidConfig,
    HarnessConfig,
    MeshConfig,
    PersonaConfig,
    PlanConfig,
    Secrets,
    TimerConfig,
)
from diploid_agent.models import ChatResult
from diploid_agent.runtime.agent_runtime import AgentRuntime
from diploid_agent.transport.http import create_app
from fastapi.testclient import TestClient
from mesh_core import MeshEnvelope, generate_keypair, sign_message


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
            timer=TimerConfig(enabled=True, interval_seconds=0.1),
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

    spy = {"record": None, "wake": None}

    def record_mock(*a, **k):
        spy["record"] = (a, k)

    def wake_mock(*a, **k):
        spy["wake"] = (a, k)
        return ChatResult(reply="ok")

    monkeypatch.setattr(runtime, "record_mesh_message", record_mock)
    monkeypatch.setattr(runtime, "wake", wake_mock)

    # reply=yes wakes a turn.
    resp = _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "Hello",
        public_pem=public,
        sender_identity={},
        vault_path=vault_path,
        reply="yes",
        msg_id="msg-yes",
    )
    assert resp.status_code == 202
    assert spy["wake"] is not None
    spy["record"] = None
    spy["wake"] = None

    # reply=no still wakes a turn.
    resp = _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "Update",
        public_pem=public,
        sender_identity={},
        vault_path=vault_path,
        reply="no",
        msg_id="msg-no",
    )
    assert resp.status_code == 202
    assert spy["wake"] is not None
    spy["record"] = None
    spy["wake"] = None

    # reply=end still wakes a turn (MCP will block mesh_send).
    resp = _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "Done",
        public_pem=public,
        sender_identity={},
        vault_path=vault_path,
        reply="end",
        msg_id="msg-end",
    )
    assert resp.status_code == 202
    assert spy["record"] is None
    assert spy["wake"] is not None
    spy["record"] = None
    spy["wake"] = None

    # DSN is terminal: record only, no wake.
    resp = _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "[mesh-dsn] delivered",
        public_pem=public,
        sender_identity={},
        vault_path=vault_path,
        is_dsn=True,
        msg_id="msg-dsn",
    )
    assert resp.status_code == 202
    assert spy["record"] is not None
    assert spy["wake"] is None
