"""diploid-mesh — bidirectional mesh integration for diploid-agent."""

from __future__ import annotations

from diploid_mesh.core import DiploidMesh
from diploid_mesh.ingress import DiploidMeshIngress, create_handler
from diploid_mesh.mcp import DiploidMeshMcpServer
from diploid_mesh.plugin import DiploidMeshPlugin

__version__ = "0.1.0"

# Diploid-agent plugin loader expects a `Plugin` class.
Plugin = DiploidMeshPlugin

# Ingress loader expects `Ingress`, `IngressHandler`, or `create_handler`.
Ingress = DiploidMeshIngress

__all__ = [
    "DiploidMesh",
    "DiploidMeshIngress",
    "DiploidMeshMcpServer",
    "DiploidMeshPlugin",
    "Ingress",
    "Plugin",
    "create_handler",
]
