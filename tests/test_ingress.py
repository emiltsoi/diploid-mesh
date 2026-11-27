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
from diploid_agent.plugins.base import TurnInfo
from diploid_agent.plugins.contexts import PromptContext
from diploid_agent.runtime.agent_runtime import AgentRuntime
from diploid_agent.transport.http import create_app
from fastapi.testclient import TestClient
from mesh_core import MeshEnvelope, generate_keypair, sign_message

from diploid_mesh.history import append_event, history_path, make_event
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
    action: str = "do",
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
        action=action,
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


def test_mesh_receive_journals_accepted_inbound_event(
    tmp_path: Path, client_runtime: tuple[TestClient, AgentRuntime]
) -> None:
    client, runtime = client_runtime
    private, public = generate_keypair()
    resp = _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "journal me",
        public_pem=public,
        sender_identity={},
        vault_path=tmp_path / "mesh-vault",
        msg_id="journal-in-1",
    )
    assert resp.status_code == 202, resp.text

    path = history_path(runtime.sessions_root, "mesh:hermes-0")
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(events) == 1
    event = events[0]
    assert event["msg_id"] == "journal-in-1"
    assert event["direction"] == "inbound"
    assert event["delivery_state"] == "inbound-wake-enqueued"
    assert event["body"] == "journal me"
    assert event["body_chars"] == len("journal me")


def test_mesh_prompt_shows_depth_conditional_history_hint(
    client_runtime: tuple[TestClient, AgentRuntime], monkeypatch
) -> None:
    _client, runtime = client_runtime
    chat_id = "mesh:hermes-0"
    prior = make_event(
        kind="message",
        msg_id="prior-1",
        ref="design-slug",
        lease_thread_id=None,
        wire_thread=None,
        sender="hermes-0",
        recipient="diploid-0",
        direction="inbound",
        action="info",
        reply="yes",
        session="chat",
        from_session="chat",
        body="earlier",
        chat_id=chat_id,
        event_ts=1.0,
        delivery_state="inbound-wake-enqueued",
    )
    append_event(runtime.sessions_root, chat_id, prior)
    mesh_plugin = _make_mesh_plugin(runtime, chat_id)
    mesh_plugin._state["current_mesh"] = {
        "sender": "hermes-0",
        "body": "current",
        "reply": "yes",
        "message_id": "current-1",
        "ref": "design-slug",
    }

    monkeypatch.setattr("diploid_mesh.plugin.MESH_THREAD_TOOL_AVAILABLE", False)
    disabled = mesh_plugin.prompt_block()
    assert disabled is not None
    assert "mesh_thread" not in disabled

    monkeypatch.setattr("diploid_mesh.plugin.MESH_THREAD_TOOL_AVAILABLE", True)
    block = mesh_plugin.prompt_block()
    assert block is not None
    assert 'mesh_thread(ref="design-slug")' in block
    assert "at least 1 prior entries" in block

    compact = mesh_plugin.prompt_block(compact=True)
    assert compact is not None
    assert "Thread history: at least 1 prior entries via `mesh_thread`." in compact


def test_mesh_receive_duplicate_is_idempotent_accept(tmp_path: Path, client: TestClient) -> None:
    """A redelivery of an already-accepted envelope returns 202 'duplicate',
    not 400 — a response lost mid-flight must not read as failure upstream."""
    private, public = generate_keypair()
    vault = tmp_path / "mesh-vault"
    first = _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "Hello from Hermes",
        public_pem=public,
        sender_identity={},
        vault_path=vault,
        msg_id="dup-msg-1",
    )
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "accepted"

    second = _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "Hello from Hermes",
        public_pem=public,
        sender_identity={},
        vault_path=vault,
        msg_id="dup-msg-1",
    )
    assert second.status_code == 202, second.text
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["delivery_id"] == "dup-msg-1"


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

    events = [
        json.loads(line)
        for line in history_path(runtime.sessions_root, "mesh:hermes-0").read_text().splitlines()
    ]
    by_id = {event["msg_id"]: event for event in events}
    assert by_id["msg-yes"]["delivery_state"] == "inbound-wake-enqueued"
    assert by_id["msg-yes"]["kind"] == "message"
    assert by_id["msg-dsn"]["delivery_state"] == "inbound-dsn-recorded"
    assert by_id["msg-dsn"]["kind"] == "dsn"
    assert by_id["msg-dsn-header"]["delivery_state"] == "inbound-dsn-recorded"


def _make_mesh_plugin(runtime: AgentRuntime, chat_id: str) -> DiploidMeshPlugin:
    config = PluginConfig(
        name="mesh",
        module="diploid_mesh",
        prompt_slot="mesh",
        state_file="chat_mesh_state.json",
        enabled=True,
    )
    return DiploidMeshPlugin(config, chat_id, runtime.sessions_root, runtime=runtime)


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


def test_mesh_send_tracker_floats_to_telegram(tmp_path: Path, monkeypatch) -> None:
    tracker = MeshSendTracker(
        chat_id="424242",
        harness_url="http://127.0.0.1:4003",
        state_path=tmp_path / "state.json",
        api_key="secret",
        max_sends=3,
        max_message_in_turn_suggestion=2,
    )
    mock_client = MagicMock()
    tracker._client = mock_client

    tracker.notify_telegram(
        sender="peer",
        recipient="agent",
        body="pong",
        action="info",
        reply="end",
        msg_id="msg-123",
    )

    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "/mesh/424242/notify"
    assert call_args.kwargs["json"] == {
        "sender": "peer",
        "recipient": "agent",
        "body": "pong",
        "action": "info",
        "reply": "end",
        "msg_id": "msg-123",
    }
    assert call_args.kwargs["headers"]["X-API-Key"] == "secret"


def test_mesh_plugin_after_turn_merges_child_writes(
    client_runtime: tuple[TestClient, AgentRuntime],
) -> None:
    _client, runtime = client_runtime
    chat_id = "mesh:hermes-0"
    mesh_plugin = _make_mesh_plugin(runtime, chat_id)

    # Simulate the MCP child writing an outbound thread record during the turn,
    # while the plugin still has an in-memory current_mesh.
    child_state = {
        "current_mesh": {
            "sender": "hermes-0",
            "body": "ping",
            "reply": "yes",
            "message_id": "msg-1",
            "session": "chat",
            "from_session": "chat",
            "direction": "inbound",
        },
        "mesh_threads": {
            "hermes-0": {
                "sender": "agent",
                "recipient": "hermes-0",
                "message_id": "msg-2",
                "session": "chat",
                "from_session": "chat",
                "ref": "msg-1",
                "direction": "outbound",
                "action": "info",
                "reply": "yes",
            }
        },
    }
    mesh_plugin.state_path().parent.mkdir(parents=True, exist_ok=True)
    mesh_plugin.state_path().write_text(json.dumps(child_state))

    mesh_plugin.after_turn(
        TurnInfo(
            chat_id=chat_id,
            session_id="s1",
            session_number=1,
            turn_number=1,
            updated_at=0.0,
            last_stop_reason=None,
            user_message="ping",
            reply="pong",
        )
    )

    final = json.loads(mesh_plugin.state_path().read_text(encoding="utf-8"))
    assert "current_mesh" not in final
    assert final["mesh_threads"]["hermes-0"]["message_id"] == "msg-2"


def test_mesh_prompt_block_recent_threads(
    client_runtime: tuple[TestClient, AgentRuntime],
) -> None:
    _client, runtime = client_runtime
    mesh_plugin = _make_mesh_plugin(runtime, "mesh:hermes-0")

    now = time.time()
    mesh_plugin._state["mesh_threads"] = {
        "peer": {
            "sender": "peer",
            "body": "Agreed, Agent — yes/no/end by topic, not by politeness.",
            "reply": "end",
            "direction": "inbound",
            "_arrived_at": now,
        },
        "peer2": {
            "sender": "agent",
            "body": "We just negotiated a mesh reply convention.",
            "reply": "no",
            "direction": "outbound",
            "_sent_at": now - 10,
        },
    }

    block = mesh_plugin.prompt_block()
    assert block is not None
    assert "## Recent mesh" in block
    assert "peer" in block
    assert "Agreed, Agent" in block
    assert "outbound" in block


def test_mesh_prompt_block_recent_threads_compact(
    client_runtime: tuple[TestClient, AgentRuntime],
) -> None:
    _client, runtime = client_runtime
    mesh_plugin = _make_mesh_plugin(runtime, "mesh:hermes-0")

    mesh_plugin._state["mesh_threads"] = {
        "peer": {
            "sender": "peer",
            "body": "Agreed, Agent — yes/no/end by topic, not by politeness.",
            "reply": "end",
            "direction": "inbound",
            "_arrived_at": time.time(),
        }
    }

    block = mesh_plugin.prompt_block(compact=True)
    assert block is not None
    assert "## Recent mesh" in block
    assert "peer" in block


def test_mesh_prompt_block_recent_threads_shows_age(
    client_runtime: tuple[TestClient, AgentRuntime],
) -> None:
    _client, runtime = client_runtime
    mesh_plugin = _make_mesh_plugin(runtime, "mesh:hermes-0")

    mesh_plugin._state["mesh_threads"] = {
        "peer": {
            "sender": "peer",
            "body": "closing the convention thread.",
            "reply": "end",
            "direction": "inbound",
            "_arrived_at": time.time() - 7200,
        }
    }

    block = mesh_plugin.prompt_block()
    assert block is not None
    assert "2h ago" in block


def test_mesh_prompt_block_recent_threads_excludes_actionable(
    client_runtime: tuple[TestClient, AgentRuntime],
) -> None:
    """reply=yes threads are actionable and surface via the CTA, not the digest."""
    _client, runtime = client_runtime
    mesh_plugin = _make_mesh_plugin(runtime, "mesh:hermes-0")

    now = time.time()
    mesh_plugin._state["mesh_threads"] = {
        "peer": {
            "sender": "peer",
            "body": "please answer this actionable thread",
            "reply": "yes",
            "direction": "inbound",
            "_arrived_at": now,
        },
        "agent0": {
            "sender": "agent0",
            "body": "one-way status update",
            "reply": "no",
            "direction": "inbound",
            "_arrived_at": now - 60,
        },
    }

    block = mesh_plugin.prompt_block()
    assert block is not None
    assert "## Recent mesh" in block
    assert "agent0" in block
    assert "please answer this actionable thread" not in block


def test_mesh_coalesce_parks_info_no_reply(
    tmp_path: Path, client_runtime: tuple[TestClient, AgentRuntime]
) -> None:
    """info+reply=no folds into an armed mesh_digest event, not a per-message
    turn; two same-peer arrivals inside the window concatenate messages."""
    client, runtime = client_runtime
    runtime.config.harness.mesh.coalesce.enabled = True
    private, public = generate_keypair()
    vault = tmp_path / "mesh-vault"
    for i, mid in enumerate(("c-1", "c-2")):
        resp = _sign_and_send(
            client,
            private,
            "hermes-0",
            "diploid-0",
            f"note {i}",
            public_pem=public,
            sender_identity={},
            vault_path=vault,
            reply="no",
            action="info",
            msg_id=mid,
        )
        assert resp.status_code == 202, resp.text
    digest = runtime.wake_queue.get("mesh-digest:mesh:hermes-0:hermes-0:c-1")
    assert digest is not None
    assert digest.reason == "mesh_digest"
    assert [m["display"] for m in digest.payload["messages"]] == [
        "[hermes-0] note 0",
        "[hermes-0] note 1",
    ]
    assert digest.scheduled_at > time.time()  # window armed, not fired
    assert runtime.wake_queue.get("mesh:c-1") is None  # no per-envelope turn

    events = [
        json.loads(line)
        for line in history_path(runtime.sessions_root, "mesh:hermes-0").read_text().splitlines()
    ]
    by_id = {event["msg_id"]: event for event in events}
    assert by_id["c-1"]["delivery_state"] == "inbound-digest-queued"
    assert by_id["c-2"]["delivery_state"] == "inbound-digest-merged"


def test_mesh_coalesce_urgent_flushes_digest(
    tmp_path: Path, client_runtime: tuple[TestClient, AgentRuntime]
) -> None:
    """An urgent delivery from the same peer lands its own turn AND forces
    the armed digest's scheduled_at to now (flush-on-priority)."""
    client, runtime = client_runtime
    runtime.config.harness.mesh.coalesce.enabled = True
    private, public = generate_keypair()
    vault = tmp_path / "mesh-vault"
    _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "fyi",
        public_pem=public,
        sender_identity={},
        vault_path=vault,
        reply="no",
        action="info",
        msg_id="c-park",
    )
    _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "urgent thing",
        public_pem=public,
        sender_identity={},
        vault_path=vault,
        reply="yes",
        action="do",
        msg_id="c-urgent",
    )
    digest = runtime.wake_queue.get("mesh-digest:mesh:hermes-0:hermes-0:c-park")
    assert digest is not None
    assert digest.scheduled_at <= time.time()  # forced due next tick
    assert digest.attempts == 0  # fire_now, not fail()
    urgent = runtime.wake_queue.get("mesh:c-urgent")
    assert urgent is not None and urgent.reason == "mesh"


def test_mesh_coalesce_off_keeps_immediate(
    tmp_path: Path, client_runtime: tuple[TestClient, AgentRuntime]
) -> None:
    """Toggle off (default): info+reply=no still wakes immediately."""
    client, runtime = client_runtime
    private, public = generate_keypair()
    _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "note",
        public_pem=public,
        sender_identity={},
        vault_path=tmp_path / "mesh-vault",
        reply="no",
        action="info",
        msg_id="c-off",
    )
    assert runtime.wake_queue.get("mesh:c-off") is not None
    digests = [e for e in runtime.wake_queue.pending() if e.reason == "mesh_digest"]
    assert digests == []


def test_mesh_coalesce_mid_lease_park_survives(
    tmp_path: Path, client_runtime: tuple[TestClient, AgentRuntime]
) -> None:
    """Agent's leg-1 pin: a parked arrival while the digest is leased
    (mid-delivery) must create a second armed digest, not overwrite the
    in-flight one — completing the delivery must not eat the new message."""
    client, runtime = client_runtime
    runtime.config.harness.mesh.coalesce.enabled = True
    private, public = generate_keypair()
    vault = tmp_path / "mesh-vault"
    _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "first",
        public_pem=public,
        sender_identity={},
        vault_path=vault,
        reply="no",
        action="info",
        msg_id="m-1",
    )
    first = runtime.wake_queue.get("mesh-digest:mesh:hermes-0:hermes-0:m-1")
    assert first is not None
    # Simulate the digest going due and being leased for its turn.
    leased = runtime.wake_queue.pop_due(now=first.scheduled_at + 1, lease_seconds=300.0)
    assert [e.id for e in leased] == [first.id]

    _sign_and_send(
        client,
        private,
        "hermes-0",
        "diploid-0",
        "second",
        public_pem=public,
        sender_identity={},
        vault_path=vault,
        reply="no",
        action="info",
        msg_id="m-2",
    )
    second = runtime.wake_queue.get("mesh-digest:mesh:hermes-0:hermes-0:m-2")
    assert second is not None  # parked under its own id, not eaten
    assert runtime.wake_queue.get(first.id).payload["messages"] == [
        m for m in first.payload["messages"]
    ]  # in-flight digest untouched by the second arrival

    runtime.wake_queue.complete(first.id)
    assert runtime.wake_queue.get(second.id) is not None  # survives complete()
    assert second.payload["messages"][0]["display"] == "[hermes-0] second"
