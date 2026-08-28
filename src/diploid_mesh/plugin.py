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
from diploid_agent.plugins.contexts import PromptBuildContext

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
            ],
            env=env,
        )

    def prompt_block(self, max_chars: int | None = None) -> str | None:
        mesh = self._state.get("current_mesh")
        if not isinstance(mesh, dict):
            # Still provide the contract block so the agent knows how to mesh.
            block = mesh_prompt_block()
        else:
            lines = [
                mesh_prompt_block(),
                "",
                f"## Active mesh message from `{mesh.get('sender', 'unknown')}`",
                f"- action: `{mesh.get('action', 'info')}`",
                f"- reply expected: `{mesh.get('reply', 'no')}`",
            ]
            if mesh.get("ref"):
                lines.append(f"- ref: `{mesh['ref']}`")
            body = mesh.get("body") or ""
            if body:
                lines.append(f"\nMessage body: {body}")
            block = "\n".join(lines)
        if max_chars and len(block) > max_chars:
            block = block[:max_chars]
        return block

    def on_waking(self, context: WakeContext) -> None:
        if context.wake_event is None:
            return
        payload = context.wake_event.payload or {}
        if context.wake_event.reason == "mesh" or payload.get("mesh"):
            mesh = payload.get("mesh", {})
            mesh["_arrived_at"] = time.time()
            self._state["current_mesh"] = mesh
            self._save_state()

    def before_build_prompt(self, context: PromptBuildContext) -> PromptBuildContext | None:
        """No-op for type compatibility; all context is surfaced via prompt_block."""
        return None

    def after_turn(self, turn: TurnInfo) -> None:
        """Clear transient mesh context once the turn is done."""
        self._state.pop("current_mesh", None)
        self._save_state()
