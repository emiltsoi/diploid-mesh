"""End-to-end wire interop test: sender DiploidMesh -> receiver diploid-agent."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import uvicorn
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
from diploid_agent.runtime.agent_runtime import AgentRuntime
from diploid_agent.transport.http import create_app
from mesh_core import generate_keypair
from mesh_core.identity import IdentityVault, MeshIdentity

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.core import DiploidMesh


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


logger = logging.getLogger(__name__)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(
    tmp_path: Path, agent_name: str, port: int, *, mesh_enabled: bool = True
) -> Config:
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
            listen_host="127.0.0.1",
            listen_port=port,
            mesh=MeshConfig(
                enabled=mesh_enabled,
                agent_name=agent_name,
                private_key_path=tmp_path / f"{agent_name}.pem",
                vault_path=tmp_path / "vault",
                allow_loopback=True,
                chat_mapping="per_sender",
                fallback_chat_id="mesh:inbox",
                ingress_module="diploid_mesh.ingress",
                mcp_enabled=False,
            ),
        ),
        secrets=Secrets(WINDSURF_API_KEY="test-key"),
    )


def _write_identity(vault_path: Path, name: str, public_key: str, url: str = "") -> None:
    vault = IdentityVault(root=vault_path)
    vault.save(
        name,
        MeshIdentity(
            id=name,
            name=name,
            role="agent",
            description="test peer",
            url=url,
            public_key=public_key,
        ),
    )


def _server_thread(
    app, host: str, port: int, started: threading.Event
) -> tuple[threading.Thread, uvicorn.Server]:
    config = uvicorn.Config(app, host=host, port=port, log_level="error")
    server = uvicorn.Server(config)

    def run() -> None:
        started.set()
        server.run()

    t = threading.Thread(target=run, daemon=True)
    return t, server


def _wait_for_server(port: int, timeout: float = 5.0) -> None:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"Server did not start on port {port}")


def test_send_mesh_message_to_diploid_agent(tmp_path: Path) -> None:
    """A DiploidMesh sender delivers a signed message to a live diploid-agent."""
    receiver_port = _free_port()
    receiver_name = "diploid-0"
    sender_name = "hermes-0"

    # Generate keypairs.
    sender_private, sender_public = generate_keypair()
    receiver_private, receiver_public = generate_keypair()

    # Write private keys for both peers.
    (tmp_path / f"{sender_name}.pem").write_text(sender_private)
    (tmp_path / f"{receiver_name}.pem").write_text(receiver_private)

    # Set up the receiver vault with the sender's public key.
    receiver_vault = tmp_path / "receiver-vault"
    _write_identity(receiver_vault, receiver_name, receiver_public)
    _write_identity(receiver_vault, sender_name, sender_public)

    # Set up the sender vault with the receiver's public key + URL.
    sender_vault = tmp_path / "sender-vault"
    _write_identity(
        sender_vault,
        receiver_name,
        receiver_public,
        url=f"http://127.0.0.1:{receiver_port}/mesh/receive",
    )

    # Build the diploid-agent receiver.
    receiver_config = _make_config(tmp_path, receiver_name, receiver_port, mesh_enabled=True)
    receiver_config.harness.mesh.vault_path = receiver_vault
    receiver_config.harness.mesh.private_key_path = tmp_path / f"{receiver_name}.pem"
    receiver_runtime = AgentRuntime(receiver_config)
    receiver_runtime.engine = FakeEngine()
    app = create_app(receiver_config, receiver_runtime)

    # Start the receiver server in a background thread.
    started = threading.Event()
    thread, server = _server_thread(app, "127.0.0.1", receiver_port, started)
    thread.start()
    started.wait(timeout=5.0)
    _wait_for_server(receiver_port)

    try:
        # Build the sender DiploidMesh.
        sender_config = _make_config(tmp_path, sender_name, 0, mesh_enabled=True)
        sender_config.harness.mesh.vault_path = sender_vault
        sender_config.harness.mesh.private_key_path = tmp_path / f"{sender_name}.pem"
        sender = DiploidMesh(DiploidMeshConfig(sender_config.harness.mesh))

        # Send a mesh message.
        body = "Hello from the sender mesh"
        result = sender.send(receiver_name, body)

        assert result.error is None, f"delivery failed: {result.error}"
        assert result.delivery_id is not None

        # Wait for the receiver turn to complete.
        time.sleep(0.5)

        # Inspect the receiver's chat memory for the mesh message.
        chat_id = f"mesh:{sender_name}"
        mgr = receiver_runtime._memory_manager(chat_id)
        transcript = mgr._load_transcript()
        assert len(transcript) >= 1, "no turn recorded"
        user_entries = [e for e in transcript if e.get("role") == "user"]
        assert user_entries, f"no user entry in transcript: {transcript}"
        assert sender_name in user_entries[-1]["content"] or body in user_entries[-1]["content"], (
            f"expected mesh message in transcript, got: {user_entries[-1]['content']}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
