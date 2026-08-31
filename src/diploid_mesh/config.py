"""Configuration bridge from diploid-agent's HarnessConfig to mesh_core."""

from __future__ import annotations

from diploid_agent.config import MeshConfig as HarnessMeshConfig
from mesh_core import MeshConfig as CoreMeshConfig


class DiploidMeshConfig:
    """Convenience wrapper that builds a mesh_core config from diploid-agent config."""

    def __init__(self, config: HarnessMeshConfig) -> None:
        self.config = config

    def to_mesh_core(self) -> CoreMeshConfig:
        return CoreMeshConfig(
            agent_name=self.config.agent_name or "diploid-0",
            private_key_path=self.config.private_key_path,
            vault_path=self.config.vault_path,
            registry_url=self.config.registry_url,
            registry_pin=self.config.registry_pin,
            allow_insecure_registry=self.config.allow_insecure_registry,
            sign_timestamp=self.config.sign_timestamp,
            allow_loopback=self.config.allow_loopback,
            chat_mapping=self.config.chat_mapping,
            fallback_chat_id=self.config.fallback_chat_id,
            chat_map=self.config.chat_map,
            delivery_retries=self.config.delivery_retries,
            delivery_backoff=self.config.delivery_backoff,
            delivery_timeout=self.config.delivery_timeout,
            replay_window_ttl=self.config.replay_window_ttl,
            replay_window_size=self.config.replay_window_size,
            rate_limit_per_minute=self.config.rate_limit_per_minute,
            outbox_enabled=self.config.outbox_enabled,
        )

    @property
    def mcp_enabled(self) -> bool:
        return self.config.mcp_enabled

    @property
    def route(self) -> str:
        return self.config.route

    @property
    def max_sends_per_turn(self) -> int:
        return self.config.max_sends_per_turn

    @property
    def max_message_in_turn_suggestion(self) -> int:
        return self.config.max_message_in_turn_suggestion
