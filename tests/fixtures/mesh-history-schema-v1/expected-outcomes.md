# Expected resolution outcomes for the schema-v1 fixtures

These notes are fixture intent, not a Lane B implementation contract.

- `design-slug` exactly matches `reply-1` and `reply-2` in `chat-a` only.
  `root-1` is intentionally unreachable from `design-slug`: it is the positive
  fragmentation/control case, not an oversight.
- `cross-session-slug` exactly matches `cross-1` in `chat-a` and `cross-2` in
  `review-a`. This is the positive cross-session reconstruction case. The two
  events also share `lease-cross` as seat-local lease evidence.
- `uuid-child` is a parent-edge case: it has `ref=uuid-root`, so bounded closure
  should connect `uuid-child` to `uuid-root`. `failed-1` has `ref=uuid-child`
  and should be visible as a non-accepted continuity attempt within that closure.
- `digest-1` covers inbound digest acceptance provenance. Its null lease is
  intentional because notices are gate-exempt.
- `local-refused-1` covers local pre-wire refusal provenance. Its null
  `lease_thread_id` is intentional because no pair claim was created for the
  attempt; the blocking lease is evidence inside `delivery_detail`, not the
  attempt's own pair lease.
