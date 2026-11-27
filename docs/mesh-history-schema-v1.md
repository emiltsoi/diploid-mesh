# Mesh history journal schema v1

`chat_mesh_history.jsonl` is an append-only local record of mesh traffic observed
by one seat. It is a continuity read-model input, not a wire protocol artifact
and not a shared transcript store. Writers must not mutate existing lines.

One JSON object per line. Schema-v1 writers emit exactly the key set below;
readers must ignore unknown fields so a later additive schema can evolve without
breaking v1 consumers.

Required fields:

| field | type | meaning |
| --- | --- | --- |
| `schema_version` | integer | currently `1` |
| `kind` | string | `message` or `dsn`; this describes the envelope kind, not conversational success |
| `origin` | string | `live` for newly observed traffic, `legacy-seed` for imported pre-journal state |
| `msg_id` | string | envelope message id |
| `ref` | string/null | envelope ref verbatim; mixed-use by design |
| `lease_thread_id` | string/null | seat-local open-thread lease id observed at capture: inbound gate row for inbound, sender-side claim or live pair row for outbound; never a wire field |
| `wire_thread` | string/null | optional wire `[thread:...]` token when captured; in pass one `null` means not captured/unknown, never proof that no wire token was present |
| `sender` | string | envelope sender |
| `recipient` | string | envelope recipient |
| `direction` | string | `inbound` or `outbound` |
| `action` | string | `do` or `info` |
| `reply` | string | `yes`, `no`, or `end` |
| `session` | string/null | destination mesh session token |
| `from_session` | string/null | reply mesh session token |
| `body` | string | full body, never write-time truncated |
| `body_chars` | integer | Unicode code-point count of `body`, as implemented by Python `len` |
| `chat_id` | string | local chat/session id owning this journal |
| `event_ts` | number | inbound: envelope acceptance observation time; outbound: send-result observation time, not wire-emission time |
| `recorded_at` | number | journal write time, seconds since epoch |
| `delivery_state` | string | controlled vocabulary below |
| `delivery_id` | string/null | transport-observed delivery id when present |
| `delivery_error` | string/null | transport-observed error reason when present |
| `delivery_detail` | object/null | typed refusal/detail object when present |

`wire_thread` note: the current mesh-core parser recognizes the token for
interop but does not expose it on `MeshEnvelope`, so pass-one diploid-mesh
writers emit `null`. Additive emission/parse exposure requires the exact
whitelisted interop verification described in the joint design note.

## Delivery-state vocabulary

Inbound states are local acceptance dispositions:

- `inbound-wake-enqueued` — regular mesh wake was enqueued;
- `inbound-digest-queued` — coalescible notice created a digest wake;
- `inbound-digest-merged` — coalescible notice merged into an armed digest;
- `inbound-dsn-recorded` — DSN was recorded without a model turn.

Outbound states are exactly what the sender observed when `mesh_send` returned:

- `outbound-recipient-accepted` — recipient ingress accepted or idempotently
  duplicate-accepted the envelope; this does not mean the recipient model has
  read it;
- `outbound-recipient-refused` — the recipient endpoint refused or rejected
  acceptance at the protocol boundary; `delivery_detail` carries the exact
  status/reason when present;
- `outbound-local-refused` — the sender-side local thread claim/send gate
  refused before any wire send; a blocking lease in `delivery_detail` is not
  the attempt's own pair lease and does not by itself populate
  `lease_thread_id`;
- `outbound-send-failed` — acceptance was not observed because resolution,
  transport, or another send failure occurred. This is not proof the recipient
  never accepted: a response can be lost after ingress.

Pass one deliberately avoids the word `delivered` as a state because the wire
observation proves recipient acceptance, not model delivery. Failed outbound
attempts are journaled in the same event stream: they are not accepted
conversation, but they are part of the seat's continuity and explain missing
replies. Lane B/readers should render them as attempts or non-accepted
continuity evidence.

Rejected inbound traffic and duplicate inbound redeliveries are not journaled
in pass one. Their absence is a policy boundary, not evidence that no such
traffic arrived.

## Integrity and privacy

Writers append only. Read tooling must treat the journal as a local observation,
not cryptographic proof of a peer's copy. The file is written beside
`chat_mesh_state.json` inside the harness `sessions_root`; in the reference
`diploid-agent` deployment those `sessions/` and `sessions-*/` trees are
git-ignored runtime state. Manual privacy remediation is operator-approved
deletion of lines by `msg_id`; no redaction tooling exists in pass one.

## Migration honesty

Pass one does not import existing `mesh_threads` routing state. Although that
state can contain the latest body for a peer, it is overwrite-latest routing
state rather than history: it lacks complete ordering, acceptance/delivery
provenance, and enough context to import without fabricating completeness. The
`legacy-seed` origin is reserved for a future honest importer, but current
writers emit only `live` events. Pre-journal history is therefore absent rather
than complete: readers and prompt surfaces must treat empty or shallow journals
as incomplete local observations, never as proof that no earlier thread exists.

Known pass-one provenance edge: if recipient acceptance is observed but a rare
post-acceptance bookkeeping exception escapes before `mesh_send` returns, the
exception fallback can journal `outbound-send-failed`. Read tooling should treat
that state as acceptance-unobserved by the sender, not as proof the recipient
never accepted.
