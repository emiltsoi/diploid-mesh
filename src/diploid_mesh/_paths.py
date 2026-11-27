"""Filesystem-path helpers shared by the plugin and MCP server."""

from __future__ import annotations

try:
    # diploid-agent >= 0.6.15 ships the Windows-safe mapping
    # (":"-stripping, reserved names, trailing dots/spaces).
    from diploid_agent._compat import safe_dir_name
except ImportError:  # diploid-agent < 0.6.15 — POSIX behaviour

    def safe_dir_name(name: str) -> str:
        return name.replace("/", "_")


__all__ = ["safe_dir_name"]
