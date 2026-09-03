"""Core DiploidMesh class that wires mesh_core to a diploid-agent runtime."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def _is_telegram_chat_id(chat_id: str | None) -> bool:
    """Return True if chat_id looks like a Telegram numeric chat id."""
    if not chat_id:
        return False
    return chat_id.lstrip("-").isdigit()


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

    def resolve_chat_id(self, sender: str, session: str | None = None) -> str:
        """Map an inbound sender/session to a diploid chat_id.

        - chat_mapping == "single" -> always fallback_chat_id.
        - chat_mapping == "per_sender" -> chat_map keyed by sender, then
          fallback_chat_id (or a mesh:<sender> chat if the fallback is not a
          real Telegram chat id).
        - chat_mapping == "session" -> chat_map keyed by session name,
          falling back to sender lookup, then fallback_chat_id (or a
          mesh:<sender> chat if the fallback is not a real Telegram chat id).

        When the fallback_chat_id is a real Telegram chat id, unmapped messages
        from known peers are routed there instead of creating phantom
        ``mesh:<sender>`` sessions.
        """
        cfg = self.config.config
        if cfg.chat_mapping == "single":
            return cfg.fallback_chat_id

        if cfg.chat_mapping == "session" and session and session in cfg.chat_map:
            return cfg.chat_map[session]

        if sender in cfg.chat_map:
            return cfg.chat_map[sender]

        # Default unmapped known peers to the configured fallback chat when it
        # is a real Telegram session. This prevents every new mesh peer from
        # spawning a phantom `mesh:<sender>` chat on diploid-agent instances.
        if _is_telegram_chat_id(cfg.fallback_chat_id):
            return cfg.fallback_chat_id

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
        session: str | None = None,
        from_session: str | None = None,
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
            session=session,
            from_session=from_session,
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
        url: str | None = None,
        role: str = "agent",
        description: str = "",
        ttl: int | None = None,
    ) -> dict:
        """Register this or another peer with the mesh-peer-registry."""
        if not self.registry:
            return {"ok": False, "error": "registry_url not configured"}
        try:
            url = url or self._default_receive_url()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        return self.registry.register(name, url, role, description, ttl)

    def deregister(self, name: str) -> dict:
        if not self.registry:
            return {"ok": False, "error": "registry_url not configured"}
        return self.registry.deregister(name)

    def sync(self, name: str | None = None) -> dict:
        return self.sync_to_vault(name)

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
        try:
            url = url or self._default_receive_url()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        return self.register(name, url, role, description, ttl)

    def _default_receive_url(self) -> str:
        """Return this agent's canonical mesh receive URL.

        Priority:
        1. The configured harness_url + /mesh/receive.
        2. The URL stored in this agent's own vault identity.

        If neither is available, fail loudly. Never fall back to a guessed
        hostname/port, because publishing a bad URL to the registry will break
        peer discovery.
        """
        if self.config.harness_url:
            return f"{self.config.harness_url.rstrip('/')}/mesh/receive"
        own = self.vault.get(self.core_config.agent_name)
        if own and own.url:
            return own.url
        raise RuntimeError(
            "Cannot determine mesh receive URL: set harness_url or register "
            "an identity with a URL before publishing."
        )

    def _harness_port_from_url(self, url: str) -> int:
        parsed = urlparse(url)
        return parsed.port or 80

    def register_local(
        self,
        name: str | None = None,
        url: str | None = None,
        role: str = "agent",
        description: str = "",
        platform: str = "diploid",
    ) -> dict:
        """Write this agent's identity to the local mesh vault."""
        name = name or self.core_config.agent_name
        if not name:
            return {"ok": False, "error": "agent name not configured"}
        try:
            url = url or self._default_receive_url()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        if not url:
            return {"ok": False, "error": "receive URL not configured"}

        identity = MeshIdentity(
            id=name,
            name=name,
            role=role,
            description=description or f"Diploid agent substrate — {name.title()}",
            url=url,
            a2a_url="",
            public_key=self.public_key,
            allow_loopback=self.core_config.allow_loopback,
            platform=platform,
        )
        path = self.vault.save(name, identity)

        # Re-read and add diploid-specific markers that mesh_core ignores but
        # other tooling can use.
        raw = self.vault._load_yaml(path) or {}
        raw["kind"] = "diploid-agent"
        raw["platform"] = platform
        raw.setdefault("mesh", {})
        raw["mesh"]["port"] = self._harness_port_from_url(url)
        raw["mesh"]["registry_url"] = self.core_config.registry_url or ""
        raw["mesh"]["allow_loopback"] = self.core_config.allow_loopback
        self.vault._save_yaml(path, raw)

        return {
            "ok": True,
            "name": name,
            "path": str(path),
            "url": url,
            "description": identity.description,
        }

    def join_mesh(self) -> dict:
        """Join the family mesh: local identity, publish, sync peers."""
        local = self.register_local()
        if not local.get("ok"):
            return local
        published = self.publish(
            role=local.get("role", "agent"),
            description=local.get("description", ""),
        )
        synced = self.sync_to_vault()
        return {
            "ok": True,
            "registered": local,
            "published": published,
            "synced": synced,
        }

    def sync_to_vault(self, name: str | None = None) -> dict:
        """Fetch peer(s) from the registry and persist them in the local vault.

        New peers are saved with the registry's public_key and inferred platform.
        Existing identity files are only updated for discovery fields
        (url, role, description). Their public_key and platform are preserved,
        because the local fleet vault is the canonical source of truth for keys
        and substrate tags; the registry is only authoritative for discovery.
        """
        if not self.registry:
            return {"ok": False, "error": "registry_url not configured"}

        peers: list[Any]
        if name:
            peer = self.registry.get_peer(name)
            peers = [peer] if peer else []
        else:
            peers = self.registry.list_peers()

        saved: list[str] = []
        updated: list[str] = []
        failed: list[dict] = []
        for peer in peers:
            try:
                identity = MeshIdentity(
                    id=peer.name,
                    name=peer.name,
                    role=peer.role or "agent",
                    description=peer.description or "",
                    url=peer.url,
                    a2a_url="",
                    public_key=peer.public_key,
                    allow_loopback=bool(getattr(peer, "allow_loopback", False)),
                    platform=self._infer_peer_platform(peer),
                )
                path = self.vault._identity_file(peer.name)
                if path.exists():
                    self._merge_identity(path, identity)
                    updated.append(peer.name)
                else:
                    self.vault.save(peer.name, identity)
                    saved.append(peer.name)
            except Exception as exc:  # noqa: BLE001
                failed.append({"name": peer.name, "error": str(exc)})

        return {
            "ok": True,
            "saved": saved,
            "updated": updated,
            "failed": failed,
            "total": len(peers),
        }

    def _merge_identity(self, path: Path, identity: MeshIdentity) -> None:
        """Update an existing identity.yaml while preserving public_key and platform.

        The local fleet vault is the canonical source of truth for an agent's
        public key and its substrate tag. The registry is only authoritative for
        discovery metadata (url, role, description), so those are updated but
        keys and platform are left untouched.
        """
        raw = self.vault._load_yaml(path) or {}
        original = copy.deepcopy(raw)
        raw["id"] = identity.id or identity.name
        raw["name"] = identity.name
        raw["role"] = identity.role
        raw["description"] = identity.description
        if identity.allow_loopback:
            raw["allow_loopback"] = True
        if not raw.get("platform") and identity.platform:
            raw["platform"] = identity.platform
        transports = raw.setdefault("transports", {})
        hermes = transports.setdefault("hermes_webhook", {})
        hermes["url"] = identity.url
        existing_auth = hermes.get("auth", {}) or {}
        if not existing_auth.get("public_key") and identity.public_key:
            hermes["auth"] = {"public_key": identity.public_key}
        if raw != original:
            self.vault._save_yaml(path, raw)
        self.vault._cache.pop(path, None)

    def _infer_peer_platform(self, peer: Any) -> str:
        """Guess the peer's substrate from its metadata or URL."""
        if getattr(peer, "platform", None):
            return peer.platform
        url = (getattr(peer, "url", "") or "").lower()
        if "/plugins/openclaw-mesh" in url or "openclaw" in url:
            return "openclaw"
        return "hermes"
