"""Stdio MCP server for diploid-mesh tools."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.core import DiploidMesh

DEFAULT_PROTOCOL_VERSION = "2024-11-05"

logger = logging.getLogger(__name__)


class MeshSendTracker:
    """Nudge + hard-cap mesh_send calls per active ACP turn.

    Reads `current_mesh.reply` from the plugin state file. If it is `end`,
    all mesh_send calls are blocked. Otherwise the agent gets a soft
    suggestion after `max_message_in_turn_suggestion` sends and a hard
    block after `max_sends_per_turn`.
    """

    def __init__(
        self,
        chat_id: str,
        harness_url: str,
        state_path: Path,
        api_key: str | None,
        max_sends: int,
        max_message_in_turn_suggestion: int,
    ) -> None:
        self.chat_id = chat_id
        effective_url = harness_url or os.getenv("HARNESS_URL", "")
        self.harness_url = effective_url.rstrip("/") if effective_url else ""
        self.state_path = state_path
        self.api_key = api_key
        self.max_sends = max(0, max_sends)
        self.suggestion_threshold = max(0, max_message_in_turn_suggestion)
        self._client: httpx.Client | None = None
        self._turn_start: float | None = None
        self._count = 0

    def _client_or_none(self) -> httpx.Client | None:
        if not self.harness_url:
            return None
        if self._client is None:
            self._client = httpx.Client(base_url=self.harness_url, timeout=5.0)
        return self._client

    def notify_telegram(
        self,
        sender: str,
        recipient: str,
        body: str,
        action: str,
        reply: str,
        msg_id: str,
    ) -> None:
        """Float a successfully sent mesh message to Telegram as a system notice.

        This call is fire-and-forget: a failure is logged but does not break the
        mesh tool result.
        """
        client = self._client_or_none()
        if client is None:
            return
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        try:
            client.post(
                f"/mesh/{self.chat_id}/notify",
                json={
                    "sender": sender,
                    "recipient": recipient,
                    "body": body,
                    "action": action,
                    "reply": reply,
                    "msg_id": msg_id,
                },
                headers=headers,
            )
        except Exception:
            logger.exception("Failed to float mesh send to Telegram for %s", self.chat_id)

    def _turn_status(self) -> dict[str, Any] | None:
        client = self._client_or_none()
        if client is None:
            return None
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        try:
            resp = client.get(f"/turn/{self.chat_id}", params={"wait": "0"}, headers=headers)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            logger.exception("MeshSendTracker failed to query /turn/%s", self.chat_id)
        return None

    def _infer_from_session(self) -> str | None:
        """Return the current turn's mesh session, if one is recorded.

        The state file is written by the DiploidMeshPlugin on wake and is the
        source of truth for which session an inbound mesh message came in on.
        """
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            mesh = data.get("current_mesh") or {}
            return mesh.get("session") or mesh.get("from_session")
        except (json.JSONDecodeError, OSError):
            return None

    def _current_mesh_reply(self) -> str | None:
        """Read current_mesh.reply from the plugin state file, if present."""
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            mesh = data.get("current_mesh") or {}
            return mesh.get("reply")
        except (json.JSONDecodeError, OSError):
            return None

    def _nudge(self, count: int, cap: int) -> str | None:
        if cap > 0 and count >= self.suggestion_threshold and count <= cap:
            if count == cap:
                return (
                    "This is the last mesh_send allowed this turn. "
                    "If you are done, use reply=end to close the thread."
                )
            return (
                f"You have sent {count} mesh message(s) this turn. "
                "If you are done, use reply=end on the next send to close the thread."
            )
        return None

    def allowed(self) -> tuple[bool, str | None]:
        # The reply value lives on disk and is available even if the harness
        # is unreachable. reply=end is an absolute block.
        incoming_reply = self._current_mesh_reply()
        if incoming_reply == "end":
            return False, "This message has reply=end; do not send a mesh reply."

        status = self._turn_status()
        if status is None:
            status = {}

        if status.get("status") != "running":
            self._count = 0
            self._turn_start = None
            return True, None

        turn_start = status.get("start_time")
        if turn_start != self._turn_start:
            self._count = 0
            self._turn_start = turn_start

        # reply=yes / reply=no share the same cap, but the prompt tells the
        # agent to avoid replying for reply=no. The hard cap is the backstop.
        cap = self.max_sends
        self._count += 1
        if cap > 0 and self._count > cap:
            return False, f"Mesh send budget ({cap}) exceeded for this turn."

        return True, self._nudge(self._count, cap)


def _error_response(req_id: Any, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32602, "message": message},
    }


def _tool_result(req_id: Any, text: str, is_error: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        },
    }


class DiploidMeshMcpServer:
    """Minimal stdio MCP server backed by DiploidMesh."""

    def __init__(self, config: DiploidMeshConfig, args: argparse.Namespace) -> None:
        effective_harness = args.harness_url or os.getenv("HARNESS_URL", "")
        config = DiploidMeshConfig(config.config, harness_url=effective_harness)
        self.mesh = DiploidMesh(config)
        self.chat_id = args.chat_id
        if config.auto_join:
            try:
                result = self.mesh.join_mesh()
                if not result.get("ok"):
                    logger.warning("mesh auto_join returned: %s", result)
                else:
                    self._ensure_default_session_map()
            except Exception:
                logger.exception("mesh auto_join failed")
        state_dir = Path(args.sessions_root) / args.chat_id.replace("/", "_")
        state_path = state_dir / (args.state_file or "chat_mesh_state.json")
        self.tracker = MeshSendTracker(
            chat_id=args.chat_id,
            harness_url=effective_harness,
            state_path=state_path,
            api_key=os.getenv("HARNESS_API_KEY"),
            max_sends=int(os.getenv("MESH_MAX_SENDS_PER_TURN", "3")),
            max_message_in_turn_suggestion=int(
                os.getenv("MESH_MAX_MESSAGE_IN_TURN_SUGGESTION", "2")
            ),
        )

    def _infer_reply_sessions(
        self, recipient: str | None = None
    ) -> tuple[str | None, str | None]:
        """Return (outbound_session, outbound_from_session) for a mesh message.

        - If we are replying to an active mesh message, swap its session tokens
          so the reply goes back on the same door. If the inbound message only
          carried one token, use it for both directions to keep routing symmetric.
        - Otherwise this is the first message in a thread: use the session name
          that the current chat maps to for both `session` and `from_session`.
        """
        state: dict[str, Any] = {}
        if self.tracker.state_path.exists():
            try:
                state = json.loads(self.tracker.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # Prefer the active turn's mesh context, fall back to a durable thread
        # record for the recipient so multi-turn replies still work.
        mesh = state.get("current_mesh") or {}
        if not mesh and recipient:
            mesh = (state.get("mesh_threads") or {}).get(recipient) or {}

        inbound_session = mesh.get("session")
        inbound_from_session = mesh.get("from_session")

        if inbound_session or inbound_from_session:
            # Use whichever token is present as the effective session for both
            # sides, then swap: the reply is addressed to the sender's session
            # and tagged with our receiving session.
            effective = inbound_session or inbound_from_session
            session = inbound_from_session or effective
            from_session = inbound_session or effective
            return session, from_session

        # First message in a thread: route to the session this chat is mapped to.
        chat_map = self.mesh.config.config.chat_map
        for name, chat_id in chat_map.items():
            if chat_id == self.chat_id:
                return name, name
        return None, None

    def _ensure_default_session_map(self) -> None:
        """Map the default `chat` and `review` sessions to the operator chat on first join."""
        if not self.chat_id:
            return
        try:
            client = self.tracker._client_or_none()
            if client:
                client.post(
                    "/mesh/chat-map",
                    json={
                        "chat_map": {
                            "chat": self.chat_id,
                            "review": self.chat_id,
                        },
                        "chat_mapping": "session",
                    },
                    headers={"X-API-Key": self.tracker.api_key or ""},
                )
        except Exception:
            logger.warning("mesh_join: failed to set default session chat map", exc_info=True)

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "mesh_list",
                "description": "List known mesh peers from the local vault.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "mesh_send",
                "description": (
                    "Send a mesh message to another agent. Optional session selects "
                    "the receiver's named session; from_session tells the receiver "
                    "where to reply."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string"},
                        "message": {"type": "string"},
                        "action": {"type": "string", "enum": ["do", "info"], "default": "do"},
                        "reply": {
                            "type": "string",
                            "enum": ["yes", "no", "end"],
                            "default": "yes",
                        },
                        "ref": {"type": "string"},
                        "session": {"type": "string"},
                        "from_session": {"type": "string"},
                    },
                    "required": ["agent", "message"],
                },
            },
            {
                "name": "mesh_register",
                "description": "Register this agent in the local mesh vault and optionally the registry.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                        "role": {"type": "string", "default": "agent"},
                        "description": {"type": "string"},
                        "publish": {"type": "boolean", "default": True},
                    },
                },
            },
            {
                "name": "mesh_join",
                "description": "Join the family mesh: write identity to the shared vault, publish to the registry, and sync peers.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "mesh_map_session",
                "description": "Map a mesh session name to a Telegram chat id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session": {"type": "string"},
                        "chat_id": {"type": "string"},
                    },
                    "required": ["session", "chat_id"],
                },
            },
            {
                "name": "mesh_deregister",
                "description": "Deregister an agent from the mesh-peer-registry.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "mesh_sync",
                "description": "Sync peer list from the mesh-peer-registry.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                },
            },
            {
                "name": "mesh_publish",
                "description": "Publish this agent's own identity to the mesh-peer-registry.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                        "role": {"type": "string", "default": "agent"},
                        "description": {"type": "string"},
                        "ttl": {"type": "integer"},
                    },
                },
            },
            {
                "name": "mesh_health",
                "description": "Check mesh health: own key, peer count, registry reachability.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def _handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            protocol_version = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "diploid-mesh", "version": "0.1.0"},
                },
            }

        if method == "notifications/initialized":
            return None

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self._tools()}}

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                if name == "mesh_list":
                    peers = self.mesh.list_peers()
                    text = json.dumps(
                        [{"name": p.name, "role": p.role, "url": p.url} for p in peers],
                        indent=2,
                    )
                    return _tool_result(req_id, text)

                if name == "mesh_send":
                    ok, hint = self.tracker.allowed()
                    if not ok:
                        return _tool_result(req_id, f"Mesh send blocked: {hint}", is_error=True)

                    session = arguments.get("session")
                    from_session = arguments.get("from_session")
                    if session is None or from_session is None:
                        inferred_session, inferred_from_session = self._infer_reply_sessions(
                            arguments.get("agent")
                        )
                        if session is None:
                            session = inferred_session
                        if from_session is None:
                            from_session = inferred_from_session

                    result = self.mesh.send(
                        recipient=arguments["agent"],
                        body=arguments["message"],
                        action=arguments.get("action", "do"),
                        reply=arguments.get("reply", "yes"),
                        ref=arguments.get("ref"),
                        session=session,
                        from_session=from_session,
                    )
                    if result.error:
                        return _tool_result(
                            req_id, f"Delivery failed: {result.error}", is_error=True
                        )
                    self.tracker.notify_telegram(
                        sender=self.mesh.core_config.agent_name,
                        recipient=arguments["agent"],
                        body=arguments["message"],
                        action=arguments.get("action", "do"),
                        reply=arguments.get("reply", "yes"),
                        msg_id=result.delivery_id or "",
                    )
                    out = f"Delivered: {result.delivery_id}"
                    if hint:
                        out = f"{out}\n\nNote: {hint}"
                    return _tool_result(req_id, out)

                if name == "mesh_register":
                    url = arguments.get("url")
                    local = self.mesh.register_local(
                        name=arguments.get("name"),
                        url=url,
                        role=arguments.get("role", "agent"),
                        description=arguments.get("description", ""),
                    )
                    if not local.get("ok"):
                        return _tool_result(req_id, json.dumps(local), is_error=True)
                    if arguments.get("publish", True):
                        published = self.mesh.publish(
                            name=local["name"],
                            url=local["url"],
                            role=arguments.get("role", "agent"),
                            description=arguments.get("description", ""),
                        )
                        local["published"] = published
                    return _tool_result(req_id, json.dumps(local, indent=2))

                if name == "mesh_join":
                    result = self.mesh.join_mesh()
                    self._ensure_default_session_map()
                    return _tool_result(req_id, json.dumps(result, indent=2))

                if name == "mesh_map_session":
                    session = arguments["session"]
                    chat_id = arguments["chat_id"]
                    client = self.tracker._client_or_none()
                    if client is None:
                        return _tool_result(req_id, "No harness URL", is_error=True)
                    resp = client.post(
                        "/mesh/chat-map",
                        json={"chat_map": {session: chat_id}},
                        headers={"X-API-Key": self.tracker.api_key or ""},
                    )
                    if resp.status_code != 200:
                        return _tool_result(
                            req_id, f"Failed: {resp.status_code} {resp.text}", is_error=True
                        )
                    return _tool_result(req_id, json.dumps({"ok": True, "session": session, "chat_id": chat_id}))

                if name == "mesh_deregister":
                    result = self.mesh.deregister(arguments["name"])
                    return _tool_result(req_id, json.dumps(result, indent=2))

                if name == "mesh_sync":
                    result = self.mesh.sync_to_vault(arguments.get("name"))
                    return _tool_result(req_id, json.dumps(result, indent=2))

                if name == "mesh_publish":
                    result = self.mesh.publish(
                        name=arguments.get("name"),
                        url=arguments.get("url"),
                        role=arguments.get("role", "agent"),
                        description=arguments.get("description", ""),
                        ttl=arguments.get("ttl"),
                    )
                    return _tool_result(req_id, json.dumps(result, indent=2))

                if name == "mesh_health":
                    text = (
                        f"Agent: {self.mesh.core_config.agent_name}\n"
                        f"Public key: {self.mesh.public_key[:60]}...\n"
                        f"Local peers: {len(self.mesh.list_peers())}\n"
                        f"Registry: {'configured' if self.mesh.registry else 'not configured'}"
                    )
                    return _tool_result(req_id, text)

                return _error_response(req_id, f"Unknown tool: {name}")
            except Exception as exc:
                logger.exception("mesh tool %s failed", name)
                return _tool_result(req_id, f"Error: {exc}", is_error=True)

        return _error_response(req_id, f"Unknown method: {method}")

    def run(self) -> None:
        """Read JSON-RPC lines from stdin and write responses to stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self._handle(request)
            if response is not None:
                print(json.dumps(response), flush=True)


def _mesh_config_from_env(harness_url: str = "") -> DiploidMeshConfig:
    """Build a DiploidMeshConfig from environment overrides and defaults."""
    from diploid_agent.config import MeshConfig

    mesh = MeshConfig()
    if os.getenv("MESH_ENABLED"):
        mesh.enabled = os.getenv("MESH_ENABLED", "").lower() in ("1", "true", "yes")
    if os.getenv("MESH_AGENT_NAME"):
        mesh.agent_name = os.getenv("MESH_AGENT_NAME")
    if os.getenv("MESH_PRIVATE_KEY_PATH"):
        mesh.private_key_path = Path(os.getenv("MESH_PRIVATE_KEY_PATH"))
    if os.getenv("MESH_VAULT_PATH"):
        mesh.vault_path = Path(os.getenv("MESH_VAULT_PATH"))
    if os.getenv("MESH_REGISTRY_URL"):
        mesh.registry_url = os.getenv("MESH_REGISTRY_URL")
    if os.getenv("MESH_REGISTRY_PIN"):
        mesh.registry_pin = os.getenv("MESH_REGISTRY_PIN")
    if os.getenv("MESH_ALLOW_LOOPBACK"):
        mesh.allow_loopback = (
            os.getenv("MESH_ALLOW_LOOPBACK", "").lower() in ("1", "true", "yes")
        )
    if os.getenv("MESH_CHAT_MAPPING"):
        mesh.chat_mapping = os.getenv("MESH_CHAT_MAPPING")
    if os.getenv("MESH_FALLBACK_CHAT_ID"):
        mesh.fallback_chat_id = os.getenv("MESH_FALLBACK_CHAT_ID")
    if os.getenv("MESH_CHAT_MAP"):
        try:
            mesh.chat_map = json.loads(os.getenv("MESH_CHAT_MAP"))
        except json.JSONDecodeError:
            pass
    if os.getenv("MESH_AUTO_JOIN"):
        mesh.auto_join = os.getenv("MESH_AUTO_JOIN", "").lower() in ("1", "true", "yes")
    return DiploidMeshConfig(mesh, harness_url=harness_url)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diploid Mesh MCP server")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--sessions-root", required=True)
    parser.add_argument("--state-file", default="chat_mesh_state.json")
    parser.add_argument(
        "--harness-url",
        default=os.getenv("HARNESS_URL", ""),
        help="URL of the diploid-agent harness. Defaults to HARNESS_URL env var.",
    )
    args = parser.parse_args()

    mesh_config = _mesh_config_from_env(args.harness_url)
    server = DiploidMeshMcpServer(mesh_config, args)
    server.run()


if __name__ == "__main__":
    main()
