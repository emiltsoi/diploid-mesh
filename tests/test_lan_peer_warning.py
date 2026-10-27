"""Startup warning for LAN peers unreachable without allow_loopback."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from diploid_agent.config import MeshConfig
from mesh_core import IdentityVault
from mesh_core.identity import MeshIdentity

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.core import DiploidMesh


def _seed_peer(vault_path: Path, name: str, url: str, *, allow_loopback: bool = False) -> None:
    IdentityVault(root=vault_path).save(
        name,
        MeshIdentity(
            id=name,
            name=name,
            role="agent",
            description="test",
            url=url,
            allow_loopback=allow_loopback,
        ),
    )


def _mesh(tmp_path: Path, *, allow_loopback: bool = False) -> DiploidMesh:
    cfg = MeshConfig(
        agent_name="receiver",
        private_key_path=tmp_path / "receiver.pem",
        vault_path=tmp_path / "vault",
        allow_loopback=allow_loopback,
    )
    return DiploidMesh(DiploidMeshConfig(cfg))


def test_warns_for_lan_peer_without_allow(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_peer(tmp_path / "vault", "lily", "http://192.168.1.48:8760/mesh/receive")
    with caplog.at_level(logging.WARNING, logger="diploid_mesh.core"):
        _mesh(tmp_path)
    assert "1 vault peer(s) are LAN-addressed but unreachable" in caplog.text
    assert "lily" in caplog.text


def test_silent_when_global_allow(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_peer(tmp_path / "vault", "lily", "http://192.168.1.48:8760/mesh/receive")
    with caplog.at_level(logging.WARNING, logger="diploid_mesh.core"):
        _mesh(tmp_path, allow_loopback=True)
    assert "LAN-addressed" not in caplog.text


def test_silent_when_peer_flag_set(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_peer(
        tmp_path / "vault",
        "lily",
        "http://192.168.1.48:8760/mesh/receive",
        allow_loopback=True,
    )
    with caplog.at_level(logging.WARNING, logger="diploid_mesh.core"):
        _mesh(tmp_path)
    assert "LAN-addressed" not in caplog.text


def test_silent_for_wan_peer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_peer(tmp_path / "vault", "remote", "https://8.8.8.8/mesh/receive")
    with caplog.at_level(logging.WARNING, logger="diploid_mesh.core"):
        _mesh(tmp_path)
    assert "LAN-addressed" not in caplog.text
