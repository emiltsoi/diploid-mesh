"""Tests for DiploidMesh.resolve_chat_id."""

from pathlib import Path

import pytest
from diploid_agent.config import MeshConfig

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.core import DiploidMesh


@pytest.fixture
def tmp_vault_path(tmp_path: Path) -> Path:
    return tmp_path / "vault"


def _mesh(
    chat_mapping: str, fallback: str, chat_map: dict[str, str], tmp_path: Path
) -> DiploidMesh:
    cfg = MeshConfig(
        enabled=True,
        agent_name="diploid-0",
        private_key_path=tmp_path / "test.pem",
        vault_path=tmp_path / "vault",
        registry_url=None,
        allow_insecure_registry=True,
        allow_loopback=True,
        chat_mapping=chat_mapping,
        fallback_chat_id=fallback,
        chat_map=chat_map,
        auto_join=False,
        mcp_enabled=False,
    )
    config = DiploidMeshConfig(cfg)
    mesh = DiploidMesh(config)
    mesh.is_known = lambda name: name == "linda"  # type: ignore[method-assign]
    return mesh


def test_session_mapping_uses_session_key(tmp_path: Path) -> None:
    mesh = _mesh("session", "7945905361", {"chat": "7945905361", "review": "12345"}, tmp_path)
    assert mesh.resolve_chat_id("linda", session="review") == "12345"


def test_session_mapping_defaults_to_telegram_fallback_for_known_peer(tmp_path: Path) -> None:
    mesh = _mesh("session", "7945905361", {"chat": "7945905361"}, tmp_path)
    assert mesh.resolve_chat_id("linda", session="unknown") == "7945905361"


def test_session_mapping_defaults_to_mesh_chat_for_non_telegram_fallback(tmp_path: Path) -> None:
    mesh = _mesh("session", "mesh:inbox", {"chat": "7945905361"}, tmp_path)
    assert mesh.resolve_chat_id("linda", session="unknown") == "mesh:linda"


def test_per_sender_mapping_uses_sender_key(tmp_path: Path) -> None:
    mesh = _mesh("per_sender", "7945905361", {"linda": "12345"}, tmp_path)
    assert mesh.resolve_chat_id("linda") == "12345"


def test_per_sender_mapping_defaults_to_telegram_fallback_for_known_peer(tmp_path: Path) -> None:
    mesh = _mesh("per_sender", "7945905361", {}, tmp_path)
    assert mesh.resolve_chat_id("linda") == "7945905361"


def test_unknown_sender_still_falls_back(tmp_path: Path) -> None:
    mesh = _mesh("session", "7945905361", {}, tmp_path)
    mesh.is_known = lambda name: False  # type: ignore[method-assign]
    assert mesh.resolve_chat_id("stranger", session="chat") == "7945905361"
