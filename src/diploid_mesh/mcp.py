"""Stdio MCP server for diploid-mesh tools."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.core import DiploidMesh

DEFAULT_PROTOCOL_VERSION = "2024-11-05"

logger = logging.getLogger(__name__)


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

    def __init__(self, config: DiploidMeshConfig) -> None:
        self.mesh = DiploidMesh(config)

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "mesh_list",
                "description": "List known mesh peers from the local vault.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "mesh_send",
                "description": "Send a mesh message to another agent.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string"},
                        "message": {"type": "string"},
                        "action": {"type": "string", "enum": ["do", "info"], "default": "do"},
                        "reply": {"type": "string", "enum": ["yes", "no", "end"], "default": "yes"},
                        "ref": {"type": "string"},
                    },
                    "required": ["agent", "message"],
                },
            },
            {
                "name": "mesh_register",
                "description": "Register an agent with the mesh-peer-registry.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                        "role": {"type": "string", "default": "agent"},
                        "description": {"type": "string"},
                        "ttl": {"type": "integer"},
                    },
                    "required": ["name", "url"],
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
                    result = self.mesh.send(
                        recipient=arguments["agent"],
                        body=arguments["message"],
                        action=arguments.get("action", "do"),
                        reply=arguments.get("reply", "yes"),
                        ref=arguments.get("ref"),
                    )
                    if result.error:
                        return _tool_result(
                            req_id, f"Delivery failed: {result.error}", is_error=True
                        )
                    return _tool_result(req_id, f"Delivered: {result.delivery_id}")

                if name == "mesh_register":
                    result = self.mesh.register(
                        name=arguments["name"],
                        url=arguments["url"],
                        role=arguments.get("role", "agent"),
                        description=arguments.get("description", ""),
                        ttl=arguments.get("ttl"),
                    )
                    return _tool_result(req_id, json.dumps(result, indent=2))

                if name == "mesh_deregister":
                    result = self.mesh.deregister(arguments["name"])
                    return _tool_result(req_id, json.dumps(result, indent=2))

                if name == "mesh_sync":
                    result = self.mesh.sync(arguments.get("name"))
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


def _mesh_config_from_env() -> DiploidMeshConfig:
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
    return DiploidMeshConfig(mesh)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diploid Mesh MCP server")
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--sessions-root", required=True)
    parser.add_argument("--state-file", default="chat_mesh_state.json")
    args = parser.parse_args()

    # args.sessions_root is currently unused; the MCP process is stateless.
    # It is accepted for forward-compatibility with per-chat state files.
    _ = args.sessions_root

    mesh_config = _mesh_config_from_env()
    server = DiploidMeshMcpServer(mesh_config)
    server.run()


if __name__ == "__main__":
    main()
