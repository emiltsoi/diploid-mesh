# diploid-mesh

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Bidirectional mesh integration for [diploid-agent](https://github.com/emiltsoi/diploid-agent). It makes a diploid-agent a full mesh peer, compatible with [hermes-mesh](https://github.com/emiltsoi/hermes-mesh) and [openclaw-mesh](https://github.com/emiltsoi/openclaw-mesh).

## What it does

- Receives Ed25519-signed `[mesh]` webhooks on `/mesh/receive` (and the OpenClaw alias `/plugins/openclaw-mesh/webhook`).
- Wakes the diploid runtime with mesh context so the agent can reply.
- Exposes MCP tools (`mesh_send`, `mesh_list`, `mesh_register`, `mesh_sync`, `mesh_publish`, `mesh_health`, `mesh_deregister`).
- Enforces the mesh contract: replay windows, `THREAD_CLOSED`, DSN exemption, and `reply=end` terminal semantics.
- Stores per-chat mesh state (`chat_mesh_state.json`) and a prompt block teaching the agent the CTA contract.
- Relies on [`mesh-peer-registry`](https://github.com/emiltsoi/mesh-peer-registry) for shared envelope parsing, identity, crypto, and registry primitives.

## Install

### From PyPI (recommended)

```bash
pip install diploid-mesh
```

`diploid-mesh` requires `diploid-agent>=0.4.0` and `mesh-peer-registry>=0.1.7`.

### From source

```bash
pip install -e /path/to/mesh-peer-registry
pip install -e /path/to/diploid-mesh
```

## Configure diploid-agent

Add to `harness.yaml`:

```yaml
harness:
  mesh:
    enabled: true
    agent_name: diploid-0
    private_key_path: ~/.mesh/keys/diploid-0.pem
    vault_path: ~/.mesh
    registry_url: http://127.0.0.1:8646
    chat_mapping: per_sender
    fallback_chat_id: mesh:inbox
    ingress_module: diploid_mesh.ingress
    mcp_enabled: true
  plugins:
    - name: mesh
      enabled: true
      module: diploid_mesh
      prompt_slot: persona_state
      first_prompt_only: false
      prompt_order: 50
      max_prompt_chars: 4096
      state_file: chat_mesh_state.json
```

## Prepare the vault

```bash
mkdir -p ~/.mesh/agents/hermes-0
```

Write `~/.mesh/agents/hermes-0/identity.yaml`:

```yaml
id: hermes-0
name: hermes-0
description: Hermes gateway peer
role: gateway
transports:
  hermes_webhook:
    protocol: hermes-webhook
    url: http://127.0.0.1:8123/mesh/receive
    auth:
      public_key: |
        -----BEGIN PUBLIC KEY-----
        ...
        -----END PUBLIC KEY-----
```

And the reciprocal identity for `diploid-0` in `~/.mesh/agents/diploid-0/identity.yaml`.

## Test

```bash
python -m pytest
```

Fleet interop tests (require a running Hermes gateway) are marked with `pytest.mark.fleet`:

```bash
python -m pytest -m fleet
```

## Project links

- Source: <https://github.com/emiltsoi/diploid-mesh>
- Mesh protocol / shared primitives: <https://github.com/emiltsoi/mesh-peer-registry>
- Hermes gateway bridge: <https://github.com/emiltsoi/hermes-mesh>
- OpenClaw bridge: <https://github.com/emiltsoi/openclaw-mesh>

## License

[MIT](LICENSE)
