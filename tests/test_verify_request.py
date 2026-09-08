"""Regression tests for DiploidMesh.verify_request rejection ordering."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from diploid_agent.config import MeshConfig
from mesh_core import MeshEnvelope, generate_keypair, sign_message
from mesh_core.identity import MeshIdentity
from mesh_core.threads import record as record_close

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.core import DiploidMesh


def _mesh(tmp_path: Path) -> DiploidMesh:
    cfg = MeshConfig(
        agent_name="receiver",
        private_key_path=tmp_path / "receiver.pem",
        vault_path=tmp_path / "vault",
        allow_loopback=True,
    )
    return DiploidMesh(DiploidMeshConfig(cfg))


def _signed_request(
    *,
    sender_private: str,
    sender: str,
    recipient: str,
    body: str,
    msg_id: str,
    ref: str | None = None,
    reply: str = "yes",
) -> tuple[dict, bytes]:
    envelope = MeshEnvelope(
        sender=sender,
        recipient=recipient,
        msg_id=msg_id,
        action="do",
        reply=reply,
        ref=ref,
        body=body,
    )
    ts = str(time.time())
    body_json = json.dumps({"from": sender, "text": envelope.build()}, sort_keys=True)
    signature = sign_message(sender_private, f"{ts}\n{body_json}".encode())
    headers = {"X-Mesh-Timestamp": ts, "X-Mesh-Signature": signature}
    return headers, body_json.encode()


def _register_sender(mesh: DiploidMesh, sender: str, public_pem: str) -> None:
    mesh.vault.save(
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


def test_thread_closed_rejection_does_not_poison_replay(tmp_path: Path) -> None:
    """A rejected envelope must not be marked seen — retries of a closed-ref
    delivery must keep surfacing THREAD_CLOSED, not degrade to 'Replay'."""
    mesh = _mesh(tmp_path)
    private, public = generate_keypair()
    _register_sender(mesh, "vesper", public)
    record_close(
        "closed-anchor-verify-1",
        "vesper",
        vault_path=mesh.core_config.vault_path,
    )

    headers, body = _signed_request(
        sender_private=private,
        sender="vesper",
        recipient="receiver",
        body="hello",
        msg_id="closed-ref-msg-1",
        ref="closed-anchor-verify-1",
    )

    with pytest.raises(ValueError, match="THREAD_CLOSED"):
        mesh.verify_request(headers, body)
    # The retry must hit the same rejection, not a replay error.
    with pytest.raises(ValueError, match="THREAD_CLOSED"):
        mesh.verify_request(headers, body)
    assert not mesh.replay.has("closed-ref-msg-1")


def test_accepted_message_is_replay_protected(tmp_path: Path) -> None:
    """A valid envelope is still marked seen and a redelivery is rejected."""
    mesh = _mesh(tmp_path)
    private, public = generate_keypair()
    _register_sender(mesh, "vesper", public)

    headers, body = _signed_request(
        sender_private=private,
        sender="vesper",
        recipient="receiver",
        body="hello",
        msg_id="ok-msg-1",
    )

    envelope = mesh.verify_request(headers, body)
    assert envelope.msg_id == "ok-msg-1"
    with pytest.raises(ValueError, match="Replay"):
        mesh.verify_request(headers, body)


def test_reply_end_records_close_after_seen(tmp_path: Path) -> None:
    """A reply=end envelope is accepted, marked seen, and closes the thread."""
    mesh = _mesh(tmp_path)
    private, public = generate_keypair()
    _register_sender(mesh, "vesper", public)

    headers, body = _signed_request(
        sender_private=private,
        sender="vesper",
        recipient="receiver",
        body="done",
        msg_id="end-msg-1",
        reply="end",
    )

    mesh.verify_request(headers, body)

    from mesh_core.threads import is_closed

    assert is_closed("end-msg-1", vault_path=mesh.core_config.vault_path)
    assert mesh.replay.has("end-msg-1")
    # And a new envelope ref-ing the closed anchor is rejected.
    headers2, body2 = _signed_request(
        sender_private=private,
        sender="vesper",
        recipient="receiver",
        body="late follow-up",
        msg_id="after-close-1",
        ref="end-msg-1",
    )
    with pytest.raises(ValueError, match="THREAD_CLOSED"):
        mesh.verify_request(headers2, body2)
