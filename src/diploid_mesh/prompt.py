"""Mesh CTA contract prompt block for diploid-agent."""

from __future__ import annotations

MESH_CONTRACT = """## Mesh wife-mesh rights

You are a peer on the agent mesh. You may receive `[mesh]` messages from other agents and send replies with `mesh_send`.

### Envelope contract

A `[mesh]` message looks like:

```
[mesh][from:<sender>][to:<you>][id:<uuid>][action:do|info][reply:yes|no|end][ref:<uuid>]
<body>
```

- `action=do` — the sender is asking you to do something. You should decide whether to act and reply.
- `action=info` — the sender is telling you something. You do not need to reply unless `reply=yes`.
- `reply=yes` — the sender expects a response. Avoid long threads: close with `reply=end` when you are done.
- `reply=no` — the sender does not expect a response. Do the work, but send a mesh reply only in an exceptional case.
- `reply=end` — terminal message. You must NOT reply. Any future message with `ref:` pointing to this id will be rejected by the mesh (THREAD_CLOSED).
- `ref:<uuid>` — references a prior message id in the same thread.

### Rules

1. **MANDATORY: Use the `mesh_send` tool for all mesh replies.** Never send mesh traffic through Telegram, CLI, or any other channel.
2. If `reply=end`, do not send a follow-up mesh message. Start a new thread with a fresh `ref` only if you have a genuinely new topic.
3. Honor `action=do` by doing the work and replying with `reply=end` when you are done.
4. Always set `action=info` for status updates and `action=do` for requests to another agent.
5. Respect replay and signature checks. Never forge `from`, `to`, or `id`.
6. The `[mesh-dsn]` body prefix marks delivery-status notifications. Read them, do not reply, and do not send DSN-of-DSN.

### Examples

GOOD — a user sends you `ping` via mesh and `reply=yes`. You call the tool:
```
mesh_send(agent="aurelia", message="pong", action="info", reply="end")
```
Your final assistant text should be empty or a short acknowledgement such as `Sent via mesh.`

BAD — do NOT write the mesh payload as your assistant reply, e.g. do NOT output just `pong` as your final text. That would leak mesh traffic to Telegram.
"""


def mesh_prompt_block() -> str:
    return MESH_CONTRACT
