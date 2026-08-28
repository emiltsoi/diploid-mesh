"""Core DiploidMesh class that wires mesh_core to a diploid-agent runtime."""

from __future__ import annotations

import json
import time

from mesh_core import (
    DeliveryClient,
    DeliveryResult,
    IdentityVault,
    MeshEnvelope,
    MeshIdentity,
    ReplayWindow,
    load_or_generate_keypair,
    parse_envelope,
    verify_message,
)
from mesh_core.registry import RegistryClient
from mesh_core.threads import is_closed
from mesh_core.threads import record as record_thread_close

from diploid_mesh.config import DiploidMeshConfig


class DiploidMesh:
    """Runtime handle for a diploid-agent mesh peer."""

    def __init__(self, config: DiploidMeshConfig) -> None:
        self.config = config
        self.core_config = config.to_mesh_core()
        self.vault = IdentityVault(root=self.core_config.vault_path)
        self.private_key, self.public_key = load_or_generate_keypair(
            self.core_config.agent_name,
            private_key_path_override=self.core_config.private_key_path,
        )
        self.delivery = DeliveryClient(
            private_key_pem=self.private_key,
            sign_timestamp=self.core_config.sign_timestamp,
            allow_loopback=self.core_config.allow_loopback,
            retries=self.core_config.delivery_retries,
            backoff=self.core_config.delivery_backoff,
            timeout=self.core_config.delivery_timeout,
            dsn_enabled=self.core_config.dsn_enabled,
            agent_name=self.core_config.agent_name,
        )
        self.replay = ReplayWindow(
            ttl=self.core_config.replay_window_ttl,
            max_size=self.core_config.replay_window_size,
        )
        self.registry: RegistryClient | None = None
        if self.core_config.registry_url:
            self.registry = RegistryClient(
                self.core_config.registry_url,
                self.private_key,
                self.public_key,
                allow_insecure=self.core_config.allow_insecure_registry,
                pin=self.core_config.registry_pin,
            )

    def is_known(self, name: str) -> bool:
        """Return True if the named peer is known locally or via registry."""
        if self.vault.get(name):
            return True
        if self.registry:
            try:
                peer = self.registry.get_peer(name)
            except Exception:  # noqa
                peer = None
            if peer:
                return True
        return False

    def resolve_chat_id(self, sender: str) -> str:
        """Map an inbound sender to a diploid chat_id."""
        cfg = self.core_config
        if cfg.chat_mapping == "single":
            return cfg.fallback_chat_id
        if sender in cfg.chat_map:
            return cfg.chat_map[sender]
        if self.is_known(sender):
            return f"mesh:{sender}"
        return cfg.fallback_chat_id

    def _resolve_target(self, recipient: str) -> tuple[str | None, bool]:
        """Return (target_url, allow_loopback) for a recipient."""
        identity = self.vault.get(recipient)
        if identity and identity.url:
            return identity.url, bool(identity.allow_loopback)
        if self.registry:
            try:
                peer = self.registry.get_peer(recipient)
            except Exception:  # noqa
                peer = None
            if peer:
                return peer.get("url"), peer.get("allow_loopback", False)
        return None, False

    def _sender_public_key(self, sender: str) -> str | None:
        identity = self.vault.get(sender)
        if identity and identity.public_key:
            return identity.public_key
        if self.registry:
            try:
                peer = self.registry.get_peer(sender)
            except Exception:  # noqa
                peer = None
            if peer:
                return peer.get("public_key")
        return None

    def verify_request(self, headers: dict, body: bytes) -> MeshEnvelope:
        """Verify a mesh webhook request and return the parsed envelope.

        Raises ValueError for missing/invalid signature, timestamp, or envelope.
        """
        signature = headers.get("x-mesh-signature") or headers.get("X-Mesh-Signature", "")
        timestamp = headers.get("x-mesh-timestamp") or headers.get("X-Mesh-Timestamp", "")
        if not signature or not timestamp:
            raise ValueError("Missing X-Mesh-Signature or X-Mesh-Timestamp")

        try:
            ts = float(timestamp)
        except ValueError as exc:
            raise ValueError("Invalid X-Mesh-Timestamp") from exc
        if abs(time.time() - ts) > 300:
            raise ValueError("X-Mesh-Timestamp outside replay window")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid JSON body") from exc

        text = payload.get("text", "")
        sender = payload.get("from", "unknown")
        envelope = parse_envelope(text)

        public_key = self._sender_public_key(sender)
        if not public_key:
            raise ValueError(f"No public key for sender {sender}")

        signed_body = f"{timestamp}\n{body.decode('utf-8')}".encode()
        if not verify_message(public_key, signed_body, signature):
            raise ValueError("Ed25519 signature verification failed")

        if self.replay.has(envelope.msg_id):
            raise ValueError(f"Replay: message {envelope.msg_id} already seen")
        self.replay.add(envelope.msg_id)

        # Enforce THREAD_CLOSED for non-DSN messages.
        is_dsn = (headers.get("x-mesh-dsn") or headers.get("X-Mesh-DSN", "")).lower() in (
            "1",
            "true",
            "yes",
        )
        if (
            not is_dsn
            and envelope.ref
            and is_closed(envelope.ref, vault_path=self.core_config.vault_path)
        ):
            raise ValueError(f"THREAD_CLOSED: {envelope.ref}")

        if not is_dsn and envelope.reply == "end":
            record_thread_close(
                envelope.msg_id,
                self.core_config.agent_name,
                vault_path=self.core_config.vault_path,
            )

        return envelope

    def send(
        self,
        recipient: str,
        body: str,
        *,
        action: str = "do",
        reply: str = "yes",
        ref: str | None = None,
        msg_id: str | None = None,
    ) -> DeliveryResult:
        """Send a mesh message to `recipient`."""
        import uuid

        if msg_id is None:
            msg_id = str(uuid.uuid4())
        envelope = MeshEnvelope(
            sender=self.core_config.agent_name,
            recipient=recipient,
            msg_id=msg_id,
            action=action,  # type: ignore[arg-type]
            reply=reply,  # type: ignore[arg-type]
            ref=ref,
            body=body,
        )
        target_url, allow_loopback = self._resolve_target(recipient)
        if not target_url:
            return DeliveryResult(error="not-found")

        # Allow per-peer loopback override.
        return self.delivery.send(
            envelope,
            target_url,
            dsn_from=self.core_config.agent_name,
            dsn_to=recipient,
            is_dsn=False,
            allow_loopback=allow_loopback or self.core_config.allow_loopback,
        )

    def list_peers(self) -> list[MeshIdentity]:
        """List mesh peers from the local vault."""
        return self.vault.list()

    def register(
        self,
        name: str,
        url: str,
        role: str = "agent",
        description: str = "",
        ttl: int | None = None,
    ) -> dict:
        """Register this or another peer with the mesh-peer-registry."""
        if not self.registry:
            return {"ok": False, "error": "registry_url not configured"}
        return self.registry.register(name, url, role, description, ttl)

    def deregister(self, name: str) -> dict:
        if not self.registry:
            return {"ok": False, "error": "registry_url not configured"}
        return self.registry.deregister(name)

    def sync(self, name: str | None = None) -> dict:
        if not self.registry:
            return {"ok": False, "error": "registry_url not configured"}
        if name:
            peer = self.registry.get_peer(name)
            return {"ok": True, "peer": peer}
        return {"ok": True, "peers": self.registry.list_peers()}

    def publish(
        self,
        name: str | None = None,
        url: str | None = None,
        role: str = "agent",
        description: str = "",
        ttl: int | None = None,
    ) -> dict:
        """Publish this agent's own identity to the registry."""
        name = name or self.core_config.agent_name
        if not url:
            url = f"http://{self.core_config.agent_name}:4003/mesh/receive"
        return self.register(name, url, role, description, ttl)
