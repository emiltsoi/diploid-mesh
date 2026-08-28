# Hermes fleet integration guide (Phase B)

This guide is for the Hermes fleet operators. The local wire slice (Phase A)
validates the same envelope/signature/timestamp contract; this guide enables the
real `hermes-mesh` ↔ `diploid-mesh` test.

## Why it is deferred

The local workspace does not contain the Hermes `gateway` package, which is a
runtime host for `hermes-mesh`. The `hermes-mesh` `MeshAdapter` imports from
`gateway.config`, `gateway.platforms.base`, and `gateway.session`, so it cannot
be launched without a Hermes gateway instance.

## Fleet setup for one diploid ↔ one Hermes interop test

1. Pick two agents:
   - `diploid-0` (the diploid-agent instance under test)
   - `hermes-0` (the Hermes fleet agent with `hermes-mesh` enabled)

2. Generate an Ed25519 keypair for each agent and exchange public keys.
   Use `mesh_core.crypto.generate_keypair()` or the equivalent Hermes command.

3. Populate each agent's `mesh/agents/<name>/identity.yaml`:

   On `diploid-0`:
   ```yaml
   id: hermes-0
   name: hermes-0
   role: gateway
   description: Hermes mesh peer
   transports:
     hermes_webhook:
       protocol: hermes-webhook
       url: http://<hermes-host>:<port>/mesh/receive
       auth:
         public_key: |
           -----BEGIN PUBLIC KEY-----
           ... hermes-0 public key ...
           -----END PUBLIC KEY-----
   ```

   On `hermes-0` (or its fleet vault), add `diploid-0`:
   ```yaml
   id: diploid-0
   name: diploid-0
   role: agent
   description: diploid-agent mesh peer
   transports:
     hermes_webhook:
       protocol: hermes-webhook
       url: http://<diploid-host>:4003/mesh/receive
       auth:
         public_key: |
           -----BEGIN PUBLIC KEY-----
           ... diploid-0 public key ...
           -----END PUBLIC KEY-----
   ```

4. Configure `diploid-0` `harness.yaml`:
   ```yaml
   harness:
     mesh:
       enabled: true
       agent_name: diploid-0
       private_key_path: /path/to/diploid-0.pem
       vault_path: /path/to/vault
       allow_loopback: true   # only for loopback/lab test
       chat_mapping: per_sender
       fallback_chat_id: mesh:inbox
       ingress_module: diploid_mesh.ingress
       mcp_enabled: true
   ```

5. Configure Hermes `hermes-0` profile to include `hermes-mesh`:
   ```yaml
   platforms:
     mesh:
       type: mesh
       extra:
         host: 127.0.0.1
         port: <port>
         secret: any-non-empty-value
         target_session: telegram:dm:...   # or a test DM
   ```

6. Start both agents.

## Guardrails for the fleet test

- Run on a lab/loopback host, or set `allow_loopback: true` only in the test
  config (do not commit that to production).
- Set `harness.mesh.rate_limit_per_minute: 0` (disabled) for the test, or keep
  Hermes's `MESH_RATE_LIMIT_PER_MINUTE` low.
- Point Hermes `target_session` at a single test DM/chat, not a public channel.
- Use a private `mesh-peer-registry` or isolated vault so the interop does not
  publish to a public registry.
- Verify signatures are enforced on both sides before allowing external traffic.

## Verification steps

1. From Hermes, send a mesh message to `diploid-0`:
   - `body: "hello from hermes"`
   - `action: do`
   - `reply: yes`

2. diploid-agent should wake the `mesh:hermes-0` chat and surface the message.
   You can verify via the HTTP API:
   ```bash
   curl -s http://127.0.0.1:4003/status/mesh:hermes-0
   ```

3. From diploid-agent (via MCP or HTTP), send a reply:
   - Call `mesh_send` with `agent=hermes-0`, `message="reply from diploid"`,
     `reply=end`.

4. Hermes should receive the message and route it to the configured
   `target_session`.

## Recommended final test script

Fleet-side test in `diploid-mesh/tests/test_hermes_interop.py` (not present
locally because it requires the fleet):

```python
# Place this in diploid-mesh/tests/test_hermes_interop.py on a host with both.
import pytest
import aiohttp
from diploid_mesh import DiploidMesh

@pytest.mark.fleet
async def test_hermes_to_diploid():
    mesh = DiploidMesh.from_config("diploid-0")
    result = mesh.send("hermes-0", "ping from fleet", action="do", reply="yes")
    assert result.error is None

    # Poll diploid status endpoint until message appears.
    async with aiohttp.ClientSession() as s:
        for _ in range(10):
            async with s.get("http://127.0.0.1:4003/status/mesh:hermes-0") as r:
                data = await r.json()
                if data.get("active"):
                    return
            await asyncio.sleep(0.5)
    raise AssertionError("diploid-agent did not process the mesh message")
```

## Notes

- The wire contract is `[mesh][from:<sender>][to:<recipient>][id:<uuid>][action:do|info][reply:yes|no|end]`.
- Both sides must sign with Ed25519 and include `X-Mesh-Timestamp` and
  `X-Mesh-Signature`.
- diploid-mesh supports `POST /mesh/receive` and the OpenClaw alias
  `POST /plugins/openclaw-mesh/webhook`.
