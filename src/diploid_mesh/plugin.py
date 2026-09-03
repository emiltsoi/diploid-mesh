"""Per-chat state plugin for mesh context and MCP server registration."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from diploid_agent.config import (
    McpServerConfig,
    PluginConfig,
)
from diploid_agent.config import (
    MeshConfig as HarnessMeshConfig,
)
from diploid_agent.plugins.base import StatePlugin, TurnInfo, WakeContext
from diploid_agent.plugins.contexts import PromptBuildContext, PromptContext

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.prompt import mesh_prompt_block


class DiploidMeshPlugin(StatePlugin):
    """Tracks mesh state per chat and exposes the mesh MCP server."""

    def __init__(
        self,
        config: PluginConfig,
        chat_id: str,
        sessions_root: Path,
        runtime: Any | None = None,
    ) -> None:
        super().__init__(config, chat_id, sessions_root, runtime=runtime)
        self._state: dict[str, Any] = self._load_state()

        # Prefer harness-level mesh config, but allow per-plugin overrides.
        if runtime is not None:
            harness_mesh = runtime.config.harness.mesh.model_dump()
            plugin_mesh = self.config.config.get("mesh", {})
            harness_mesh.update(plugin_mesh)
            self.mesh_config = DiploidMeshConfig(HarnessMeshConfig.model_validate(harness_mesh))
        else:
            self.mesh_config = DiploidMeshConfig(
                HarnessMeshConfig.model_validate(self.config.config.get("mesh", {}))
            )

    def state_path(self) -> Path | None:
        if not self.config.state_file:
            return None
        return self._chat_dir() / self.config.state_file

    def _chat_dir(self) -> Path:
        return self.sessions_root / self.chat_id.replace("/", "_")

    def _load_state(self) -> dict[str, Any]:
        path = self.state_path()
        if path is None or not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_state(self) -> None:
        path = self.state_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._state, indent=2, default=str))

    def mcp_server(self) -> McpServerConfig | None:
        if self.config.mcp_server:
            return self.config.mcp_server

        if not self.mesh_config.mcp_enabled:
            return None

        # Build a default MCP server config from harness mesh settings.
        mesh = self.mesh_config.config
        env: list[str] = []
        if mesh.agent_name:
            env.append(f"MESH_AGENT_NAME={mesh.agent_name}")
        if mesh.vault_path:
            env.append(f"MESH_VAULT_PATH={mesh.vault_path}")
        if mesh.registry_url:
            env.append(f"MESH_REGISTRY_URL={mesh.registry_url}")
        if mesh.private_key_path:
            env.append(f"MESH_PRIVATE_KEY_PATH={mesh.private_key_path}")
        if mesh.allow_loopback:
            env.append("MESH_ALLOW_LOOPBACK=1")
        if mesh.chat_mapping:
            env.append(f"MESH_CHAT_MAPPING={mesh.chat_mapping}")
        if mesh.fallback_chat_id:
            env.append(f"MESH_FALLBACK_CHAT_ID={mesh.fallback_chat_id}")
        if mesh.chat_map:
            env.append(f"MESH_CHAT_MAP={json.dumps(mesh.chat_map)}")
        if mesh.max_sends_per_turn is not None:
            env.append(f"MESH_MAX_SENDS_PER_TURN={mesh.max_sends_per_turn}")
        if mesh.max_message_in_turn_suggestion is not None:
            env.append(f"MESH_MAX_MESSAGE_IN_TURN_SUGGESTION={mesh.max_message_in_turn_suggestion}")

        return McpServerConfig(
            name="diploid-mesh",
            command="python",
            args=[
                "-m",
                "diploid_mesh.mcp",
                "--chat-id",
                "{chat_id}",
                "--sessions-root",
                "{sessions_root}",
                "--state-file",
                "chat_mesh_state.json",
                "--harness-url",
                "{harness_url}",
            ],
            env=env,
        )

    def prompt_block(self, max_chars: int | None = None) -> str | None:
        mesh = self._state.get("current_mesh")
        if not isinstance(mesh, dict):
            # Still provide the contract block so the agent knows how to mesh.
            block = mesh_prompt_block()
        else:
            reply = mesh.get("reply", "no")
            lines = [
                mesh_prompt_block(),
                "",
                f"## Active mesh message from `{mesh.get('sender', 'unknown')}`",
                f"- action: `{mesh.get('action', 'info')}`",
                f"- reply expected: `{reply}`",
            ]
            if mesh.get("session"):
                lines.append(f"- session: `{mesh['session']}`")
            if mesh.get("from_session"):
                lines.append(f"- from_session: `{mesh['from_session']}`")
            if mesh.get("ref"):
                lines.append(f"- ref: `{mesh['ref']}`")
            if reply == "no":
                lines.append(
                    "- **This is a one-way message. Do the work locally. Only send a mesh reply in an exceptional case."
                )
            if reply == "end":
                lines.append("- **The sender has ended this thread. Do NOT send a mesh reply.**")
            body = mesh.get("body") or ""
            if body:
                lines.append(f"\nMessage body: {body}")
            block = "\n".join(lines)
        if max_chars and len(block) > max_chars:
            block = block[:max_chars]
        return block

    def after_prompt_built(self, pctx: PromptContext) -> PromptContext | None:
        """Prepend an unambiguous, top-of-prompt CTA when a mesh reply is required."""
        mesh = self._state.get("current_mesh")
        if not isinstance(mesh, dict):
            return None

        reply = mesh.get("reply", "no")
        if reply == "end":
            # Terminal message: no reply needed, but still reinforce silence.
            cta = self._mesh_silence_cta(mesh)
        elif reply == "no":
            # One-way message: the agent should do work locally and only reply
            # in an exceptional case. We still remind it of the tool in case it
            # decides to reply.
            cta = self._mesh_tool_cta(mesh, optional=True)
        else:
            # reply == "yes" or any other value: a mesh reply is expected.
            cta = self._mesh_tool_cta(mesh, optional=False)

        if cta:
            pctx.prompt = f"{cta}\n\n{pctx.prompt}"
        return pctx

    def _mesh_tool_cta(self, mesh: dict[str, Any], optional: bool) -> str | None:
        sender = mesh.get("sender", "unknown")
        body = (mesh.get("body") or "").strip()
        lines = [
            "# SYSTEM — MESH REPLY RULE",
            f"You have an active mesh message from `{sender}`.",
        ]
        if body:
            lines.append(f"Message body: {body}")
        if optional:
            lines.append(
                "This is a one-way message. Do the work locally. "
                "If you choose to reply, you MUST use the `mesh_send` tool; "
                "do NOT put the reply in your final assistant text."
            )
        else:
            lines.append(
                "You MUST use the `mesh_send` tool for your reply. "
                "Do NOT put the mesh payload in your final assistant text. "
                "Your final assistant text should be empty or a short acknowledgement "
                "(e.g. 'Sent via mesh.')."
            )
        lines.append("Close the thread with `reply=end` when you are done.")
        return "\n".join(lines)

    def _mesh_silence_cta(self, mesh: dict[str, Any]) -> str | None:
        sender = mesh.get("sender", "unknown")
        return (
            f"# SYSTEM — MESH SILENCE RULE\n"
            f"The mesh message from `{sender}` is terminal (`reply=end`). "
            "Do NOT send a mesh reply and do not discuss its content in your assistant text."
        )

    def on_waking(self, context: WakeContext) -> None:
        if context.wake_event is None:
            return
        payload = context.wake_event.payload or {}
        if context.wake_event.reason == "mesh" or payload.get("mesh"):
            mesh = payload.get("mesh", {})
            mesh["_arrived_at"] = time.time()
            self._state["current_mesh"] = mesh
            # Keep a durable thread record keyed by sender so a reply can be
            # sent from a later ACP turn after the transient current_mesh is
            # cleared.
            threads = self._state.setdefault("mesh_threads", {})
            sender = mesh.get("sender")
            if sender:
                threads[sender] = mesh
            self._save_state()

    def before_build_prompt(self, context: PromptBuildContext) -> PromptBuildContext | None:
        """No-op for type compatibility; all context is surfaced via prompt_block."""
        return None

    def after_turn(self, turn: TurnInfo) -> None:
        """Clear transient mesh context once the turn is done.

        The durable `mesh_threads` map is preserved so multi-turn replies still
        know which session the original message arrived on.
        """
        self._state.pop("current_mesh", None)
        self._save_state()
