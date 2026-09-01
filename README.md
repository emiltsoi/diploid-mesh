# diploid-mesh

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Bidirectional mesh integration for [diploid-agent](https://github.com/emiltsoi/diploid-agent). It makes a diploid-agent a full mesh peer, compatible with [hermes-mesh](https://github.com/emiltsoi/hermes-mesh) and [openclaw-mesh](https://github.com/emiltsoi/openclaw-mesh).

## What it does

- Receives Ed25519-signed `[mesh]` webhooks on `/mesh/receive` (and the OpenClaw alias `/plugins/openclaw-mesh/webhook`).
- Wakes the diploid runtime with mesh context so the agent can reply.
- Exposes MCP tools (`mesh_send`, `mesh_list`, `mesh_register`, `mesh_sync`, `mesh_publish`, `mesh_health`, `mesh_deregister`).
- Enforces the mesh contract: replay windows, `THREAD_CLOSED`, DSN exemption, and `reply` semantics.
- Nudges and hard-caps `mesh_send` calls per ACP turn.
- Strengthens prompt discipline with a top-of-prompt `SYSTEM — MESH REPLY RULE` CTA that commands the agent to use `mesh_send` and keep mesh traffic out of normal assistant text.
- Mirrors sent mesh messages to Telegram as `System: [mesh] ...` notices when `harness.notifications.mesh_telegram_float` is enabled.
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

## Message lifecycle

- `reply=yes` (default): the recipient runs an ACP turn and may respond. The MCP server nudges the model to use `reply=end` after `max_message_in_turn_suggestion` sends and hard-blocks at `max_sends_per_turn`.
- `reply=no`: the recipient runs an ACP turn to perform work. The prompt says "only reply in an exceptional case," and the MCP server gives the same nudge/cap as `reply=yes` but the model is expected to avoid sending.
- `reply=end`: the recipient runs an ACP turn but the MCP server hard-blocks all `mesh_send` calls; this is the last message in the thread.
- DSNs (`[mesh-dsn]` body prefix): delivery-status notifications are recorded, not replied to, and do not start a turn.

## Per-turn send cap

`harness.mesh.max_sends_per_turn` hard-limits how many `mesh_send` calls the agent can make within a single active ACP turn. The default is `3`.

`harness.mesh.max_message_in_turn_suggestion` is a soft nudge threshold. After that many `mesh_send` calls, the tool result appends a note suggesting the next send use `reply=end` to close the thread. This lets the LLM infer the graceful close.

`reply=end` always overrides the cap to `0`, blocking all `mesh_send` for that turn.

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
      prompt_slot: mesh
      first_prompt_only: false
      prompt_order: 50
      max_prompt_chars: 4096
      state_file: chat_mesh_state.json
      mcp_server:
        name: diploid-mesh
        command: python
        args:
          - -m
          - diploid_mesh.mcp
          - --chat-id
          - '{chat_id}'
          - --sessions-root
          - '{sessions_root}'
          - --harness-url
          - '{harness_url}'
        env:
          - MESH_AGENT_NAME=diploid-0
          - MESH_PRIVATE_KEY_PATH=/home/diploid/.mesh/keys/diploid-0.pem
          - MESH_VAULT_PATH=/home/diploid/.mesh
```

The `diploid-mesh` MCP server must be able to call back to the harness URL. The
`--harness-url` argument is required if it is not passed via the `HARNESS_URL`
environment variable (newer `diploid-agent` versions inject `HARNESS_URL`
automatically for all MCP children).

## Floating mesh messages to Telegram

Set in `runtime-overrides.yaml` (or live via `/config`):

```yaml
notifications:
  enabled: true
  outbox_delivery: true
  mesh_telegram_float: true
```

After every successful `mesh_send`, a system message such as
`System: [mesh] diploid-0 → hermes-0: pong (action=info) (reply=end) (id=...)`
is delivered to the sender's Telegram chat, so the human operator sees the mesh
traffic without the agent leaking it into assistant text.

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

## Cross-harness mesh

The mesh is one protocol shared by three runtimes:

- **Hermes** agents use [`hermes-mesh`](https://github.com/emiltsoi/hermes-mesh), which adds a `mesh` platform adapter and `mesh_send`/`mesh_list` tools.
- **OpenClaw** agents use [`openclaw-mesh`](https://github.com/emiltsoi/openclaw-mesh), a plugin that receives `[mesh]` webhooks and injects them as agent turns.
- **diploid-agent** agents use [`diploid-mesh`](https://github.com/emiltsoi/diploid-mesh) (this repo), a state plugin that exposes the same envelope and MCP tools over the diploid harness.

All three use the same `mesh-peer-registry` server and the same on-disk vault format (`mesh/agents/<name>/identity.yaml`), so a Hermes agent can `mesh_send` to a diploid agent, and a diploid agent can reply to an OpenClaw agent, without a custom translator.

```
Hermes fleet        mesh-peer-registry       OpenClaw          diploid-agent
   │                        │                  │                  │
   │  [mesh] + Ed25519 sig  │                  │  [mesh] + sig    │
   └────────────webhook─────┼──────────────────┼────────────────▶│
                            │  public keys /   │                  │
                            │  peer URLs       │                  │
```

The registry is optional for loopback-only fleets — a shared file-based vault (`~/.mesh/agents`) is enough — but it makes multi-host discovery simple.

## Project links

- Source: <https://github.com/emiltsoi/diploid-mesh>
- Mesh protocol / shared primitives: <https://github.com/emiltsoi/mesh-peer-registry>
- Hermes gateway bridge: <https://github.com/emiltsoi/hermes-mesh>
- OpenClaw bridge: <https://github.com/emiltsoi/openclaw-mesh>

## License

[MIT](LICENSE)
