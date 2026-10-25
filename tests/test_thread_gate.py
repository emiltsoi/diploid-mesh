"""Thread-gate acceptance tests — A1–A11 of the port spec.

``fleet/shared/specs/mesh-thread-gate-port-spec.md`` §5. Each leg is
written against the rule it discriminates, per §9: a case that passes
under both a correct and an incorrect reading is not a test.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest
from diploid_agent.config import MeshConfig
from mesh_core import MeshEnvelope, generate_keypair, sign_message
from mesh_core.delivery import DeliveryResult
from mesh_core.identity import MeshIdentity
from mesh_core.threads import is_closed

from diploid_mesh import open_threads
from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.core import DiploidMesh, GateRefusal

LEASE_MINUTES = 30


def _mesh(tmp_path: Path, name: str = "receiver") -> DiploidMesh:
    cfg = MeshConfig(
        agent_name=name,
        private_key_path=tmp_path / f"{name}.pem",
        vault_path=tmp_path / "vault",
        allow_loopback=True,
    )
    return DiploidMesh(DiploidMeshConfig(cfg))


def _register_peer(mesh: DiploidMesh, name: str, public_pem: str = "", url: str = "") -> None:
    mesh.vault.save(
        name,
        MeshIdentity(
            id=name,
            name=name,
            role="agent",
            description="test",
            url=url,
            public_key=public_pem,
            allow_loopback=True,
        ),
    )


def _signed_request(
    *,
    sender_private: str,
    sender: str,
    recipient: str,
    msg_id: str,
    body: str = "hello",
    reply: str = "yes",
    ref: str | None = None,
) -> tuple[dict, bytes]:
    envelope = MeshEnvelope(
        sender=sender,
        recipient=recipient,
        msg_id=msg_id,
        action="do",
        reply=reply,
        ref=ref,
        body=body,
    )
    ts = str(time.time())
    body_json = json.dumps({"from": sender, "text": envelope.build()}, sort_keys=True)
    signature = sign_message(sender_private, f"{ts}\n{body_json}".encode())
    return {"X-Mesh-Timestamp": ts, "X-Mesh-Signature": signature}, body_json.encode()


def _gate(tmp_path: Path, sender: str, reply: str = "yes", now: float | None = None):
    return open_threads.inbound_gate(
        sender=sender,
        peer="receiver",
        reply=reply,
        vault_path=tmp_path / "vault",
        now=now,
    )


def _rows(tmp_path: Path) -> list[dict]:
    return open_threads._read_entries(open_threads._registry_path(tmp_path / "vault"))


def _seed_lease(tmp_path: Path, opened_by: str, peer: str, *, live: bool = True) -> dict:
    """Seed a lease row directly, controlling expiry."""
    path = open_threads._registry_path(tmp_path / "vault")
    now = time.time()
    entry = {
        "thread_id": f"seed-{opened_by}-{peer}",
        "opened_by": opened_by,
        "peer": peer,
        "opened_at": now - 60,
        "lease_until": now + (60 if live else -60),
        "lease_minutes": LEASE_MINUTES,
        "replies": 0,
    }
    entries = open_threads._read_entries(path)
    entries.append(entry)
    open_threads._write_entries(entries, path)
    return entry


# --- A1: second message on a live lease merges — one thread_id, not two ---


def test_a1_second_message_merges_same_thread_id(tmp_path: Path) -> None:
    first = _gate(tmp_path, "alice")
    assert first.action == "deliver"
    second = _gate(tmp_path, "alice")
    assert second.action == "deliver"
    assert second.entry["thread_id"] == first.entry["thread_id"]
    rows = [e for e in _rows(tmp_path) if {e["opened_by"], e["peer"]} == {"alice", "receiver"}]
    assert len(rows) == 1  # merged, not appended


def test_a1_second_message_refreshes_expiry(tmp_path: Path) -> None:
    first = _gate(tmp_path, "alice")
    second = _gate(tmp_path, "alice")
    assert second.entry["lease_until"] > first.entry["lease_until"]


# --- A2: opener is not busy — replies from both peers deliver ---


def test_a2_opener_is_not_busy_both_replies_deliver(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    # receiver opened threads to alice and bob — her own outbound claims.
    open_threads.claim_outbound(me="receiver", peer="alice", vault_path=vault)
    open_threads.claim_outbound(me="receiver", peer="bob", vault_path=vault)

    a = _gate(tmp_path, "alice")
    b = _gate(tmp_path, "bob")
    assert a.action == "deliver", "reply from a peer she opened to must not be refused"
    assert b.action == "deliver", "reply from the other peer must not be refused"


def test_a2b_recipient_opened_lease_does_not_busy_third_party(tmp_path: Path) -> None:
    """The §8.1 discriminator A2 alone cannot reach: with ONLY a
    recipient-opened lease live, an unrelated sender must still deliver.
    The wrong busy reading ('in any thread = busy') refuses bob here."""
    vault = tmp_path / "vault"
    open_threads.claim_outbound(me="receiver", peer="alice", vault_path=vault)

    out = _gate(tmp_path, "bob")
    assert out.action == "deliver"
    assert out.entry["opened_by"] == "bob"


# --- A3: third party refused while a lease is live — typed, with address ---


def test_a3_third_party_refused_typed_with_address(tmp_path: Path) -> None:
    lease = _seed_lease(tmp_path, "xenia", "receiver")
    out = _gate(tmp_path, "bob")
    assert out.action == "refuse"
    d = out.refusal
    for key in ("reason", "thread_id", "peer", "opened_at", "lease_until"):
        assert key in d and d[key] is not None, key
    assert d["reason"] == "busy"
    assert d["thread_id"] == lease["thread_id"]
    # The address for a busy predicament is temporal (N6).
    assert d["retry_after"] == lease["lease_until"]


def test_a3_refusal_does_not_create_claim(tmp_path: Path) -> None:
    _seed_lease(tmp_path, "xenia", "receiver")
    _gate(tmp_path, "bob")
    pairs = [e for e in _rows(tmp_path) if {e["opened_by"], e["peer"]} == {"bob", "receiver"}]
    assert pairs == []


# --- Claim-side busy: under a shared registry the claim is the contested act (§11) ---


def test_claim_outbound_refuses_when_recipient_busy(tmp_path: Path) -> None:
    """A live third-party lease on the recipient refuses the send before
    the wire. Without this check the minted pair row pair-matches the
    sender's own envelope at the receiver's gate and the wire busy-check
    never runs — the shared-file dead letter §11 warns about."""
    lease = _seed_lease(tmp_path, "xenia", "receiver")
    out = open_threads.claim_outbound(me="bob", peer="receiver", vault_path=tmp_path / "vault")
    assert out.action == "refuse"
    d = out.refusal
    assert d["reason"] == "busy"
    assert d["thread_id"] == lease["thread_id"]
    assert d["retry_after"] == lease["lease_until"]


def test_claim_outbound_refusal_mints_nothing(tmp_path: Path) -> None:
    lease = _seed_lease(tmp_path, "xenia", "receiver")
    open_threads.claim_outbound(me="bob", peer="receiver", vault_path=tmp_path / "vault")
    pairs = [e for e in _rows(tmp_path) if {e["opened_by"], e["peer"]} == {"bob", "receiver"}]
    assert pairs == []
    # The refusal must not touch the blocking row either — a claim-time
    # refresh would silently extend xenia's hold.
    blocking = next(e for e in _rows(tmp_path) if e["thread_id"] == lease["thread_id"])
    assert blocking["lease_until"] == lease["lease_until"]
    assert blocking["replies"] == 0


def test_claim_outbound_pair_merge_beats_busy(tmp_path: Path) -> None:
    """Order mirrors inbound_gate — pair first. An opener continuing her
    own thread is never self-busy, even while a third party holds a lease
    on the same recipient."""
    own = open_threads.claim_outbound(me="bob", peer="receiver", vault_path=tmp_path / "vault")
    assert own.action == "deliver" and own.created
    _seed_lease(tmp_path, "xenia", "receiver")
    out = open_threads.claim_outbound(me="bob", peer="receiver", vault_path=tmp_path / "vault")
    assert out.action == "deliver"
    assert not out.created
    assert out.entry["thread_id"] == own.entry["thread_id"]


def test_claim_outbound_recipient_opened_lease_not_busy(tmp_path: Path) -> None:
    """Claim-side mirror of A2b — the discriminator for the predicate's
    direction. The recipient having OPENED a thread to someone else is
    not busy (the §8.1 'in any thread' misreading refuses bob here); only
    a third-party lease where peer == recipient refuses."""
    _seed_lease(tmp_path, "receiver", "xenia")
    out = open_threads.claim_outbound(me="bob", peer="receiver", vault_path=tmp_path / "vault")
    assert out.action == "deliver"
    assert out.created


def test_claim_outbound_expired_busy_row_does_not_refuse(tmp_path: Path) -> None:
    _seed_lease(tmp_path, "xenia", "receiver", live=False)
    out = open_threads.claim_outbound(me="bob", peer="receiver", vault_path=tmp_path / "vault")
    assert out.action == "deliver"
    assert out.created


def test_send_refuses_at_claim_without_wire(tmp_path: Path) -> None:
    """E2E under a shared registry: send() surfaces the typed busy refusal
    as DeliveryResult(error='busy', detail=...), spends zero wire, and
    mints no claim."""
    mesh = _mesh(tmp_path, name="sender")
    _register_peer(mesh, "receiver", url="http://127.0.0.1:9/mesh/receive")
    lease = _seed_lease(tmp_path, "xenia", "receiver")

    sent: list = []
    mesh.delivery.send = lambda *a, **k: sent.append(1) or DeliveryResult(delivery_id="x")  # type: ignore[method-assign]

    r = mesh.send("receiver", "hello")
    assert r.error == "busy"
    assert r.detail["reason"] == "busy"
    assert r.detail["thread_id"] == lease["thread_id"]
    assert r.detail["reroute"]["retry_after"] == lease["lease_until"]
    assert sent == []
    pairs = [e for e in _rows(tmp_path) if {e["opened_by"], e["peer"]} == {"sender", "receiver"}]
    assert pairs == []


# --- A4 / A6: own retry after failure succeeds; slot free for others ---


def test_a4_sender_retry_after_failed_send_succeeds(tmp_path: Path) -> None:
    mesh = _mesh(tmp_path, name="sender")
    _register_peer(mesh, "receiver", url="http://127.0.0.1:9/mesh/receive")
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return DeliveryResult(error="unreachable")
        return DeliveryResult(delivery_id="ok-1")

    mesh.delivery.send = flaky  # type: ignore[method-assign]

    r1 = mesh.send("receiver", "hello")
    assert r1.error == "unreachable"
    # The failed send's claim was released — nothing is left locking the pair.
    assert _rows(tmp_path) == []

    r2 = mesh.send("receiver", "hello again")
    assert r2.error is None
    live = open_threads.live_pair("sender", "receiver", vault_path=tmp_path / "vault")
    assert live is not None


def test_a6_slot_free_after_failure(tmp_path: Path) -> None:
    # alice's attempt failed and released; bob arriving later is permitted.
    out = open_threads.inbound_gate(
        sender="alice", peer="receiver", reply="yes", vault_path=tmp_path / "vault"
    )
    assert out.action == "deliver"
    open_threads.release_pair("alice", "receiver", vault_path=tmp_path / "vault")
    assert _gate(tmp_path, "bob").action == "deliver"


# --- A5d: distributed race — exactly one winner ---


def test_a5d_concurrent_claims_exactly_one_winner(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    barrier = threading.Barrier(3)
    results = []

    def contender(sender: str) -> None:
        barrier.wait()
        results.append(
            open_threads.inbound_gate(sender=sender, peer="receiver", reply="yes", vault_path=vault)
        )

    threads = [threading.Thread(target=contender, args=(s,)) for s in ("alice", "bob")]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    winners = [r for r in results if r.action == "deliver" and r.entry]
    losers = [r for r in results if r.action == "refuse"]
    # The invariant is a count of winners, not "alice won" (spec §11 A5d).
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0].refusal["reason"] == "busy"


# --- A7 / A8: terminator exempt from busy gate; duplicate close refused ---


def test_a7_terminator_delivered_while_third_party_holds_gate(tmp_path: Path) -> None:
    _seed_lease(tmp_path, "xenia", "receiver")
    out = _gate(tmp_path, "alice", reply="end")
    assert out.action == "deliver"


def test_a7_terminator_with_no_prior_lease_still_delivers(tmp_path: Path) -> None:
    # A goodbye we cannot prove redundant must not be refused (§8.2).
    assert _gate(tmp_path, "alice", reply="end").action == "deliver"


def test_a8_second_terminator_on_closed_thread_refused(tmp_path: Path) -> None:
    mesh = _mesh(tmp_path)
    private, public = generate_keypair()
    _register_peer(mesh, "alice", public)

    h1, b1 = _signed_request(
        sender_private=private,
        sender="alice",
        recipient="receiver",
        msg_id="end-1",
        reply="end",
    )
    env = mesh.verify_request(h1, b1)
    mesh.commit_terminator(env)  # ingress records the close post-enqueue
    assert is_closed("end-1", vault_path=mesh.core_config.vault_path)

    h2, b2 = _signed_request(
        sender_private=private,
        sender="alice",
        recipient="receiver",
        msg_id="end-2",
        reply="end",
        ref="end-1",
    )
    with pytest.raises(GateRefusal) as exc:
        mesh.verify_request(h2, b2)
    assert exc.value.detail["reason"] == "thread-closed"
    assert exc.value.status_code == 400


# --- A9: two refusal predicaments carry different, followable addresses ---


def test_a9_refusal_addresses_differ_and_each_works(tmp_path: Path) -> None:
    mesh = _mesh(tmp_path)
    private, public = generate_keypair()
    _register_peer(mesh, "alice", public)
    vault = tmp_path / "vault"

    # Predicament 1 — busy: the address is the blocking lease's expiry.
    _seed_lease(tmp_path, "xenia", "receiver")
    busy = _gate(tmp_path, "alice")
    assert busy.action == "refuse"
    busy_addr = busy.refusal["retry_after"]

    # Predicament 2 — closed thread: the address comes from a REAL second
    # refusal, not a literal — resend as a new thread, dropping the ref.
    from mesh_core.threads import record as record_close

    record_close("closed-1", "xenia", vault_path=vault)
    h, b = _signed_request(
        sender_private=private,
        sender="alice",
        recipient="receiver",
        msg_id="late-1",
        ref="closed-1",
    )
    with pytest.raises(GateRefusal) as exc:
        mesh.verify_request(h, b)
    closed = exc.value.detail
    assert closed["reason"] == "thread-closed"
    closed_addr = closed["reroute"]["action"]

    # The two predicaments carry different addresses.
    assert closed_addr != busy_addr

    # Follow the busy address: after lease_until the gate opens.
    out = _gate(tmp_path, "alice", now=busy_addr + 1)
    assert out.action == "deliver"

    # Follow the closed address: resend as a new thread (no ref) delivers.
    h2, b2 = _signed_request(
        sender_private=private,
        sender="alice",
        recipient="receiver",
        msg_id="fresh-1",
    )
    env = mesh.verify_request(h2, b2)
    assert env.msg_id == "fresh-1"


# --- A10: notice to a busy recipient delivers, and never claims ---


def test_a10_notice_delivered_to_busy_recipient_no_claim(tmp_path: Path) -> None:
    _seed_lease(tmp_path, "xenia", "receiver")
    before = len(_rows(tmp_path))
    out = _gate(tmp_path, "bob", reply="no")
    assert out.action == "deliver"
    assert len(_rows(tmp_path)) == before  # no claim was written


# --- A11: failed terminator leaves the thread open and retryable ---


def test_a11_failed_terminator_keeps_pair_open_retryable(tmp_path: Path) -> None:
    mesh = _mesh(tmp_path, name="sender")
    _register_peer(mesh, "receiver", url="http://127.0.0.1:9/mesh/receive")

    # Establish a live pair on the sender's store.
    open_threads.claim_outbound(me="sender", peer="receiver", vault_path=tmp_path / "vault")

    mesh.delivery.send = lambda *a, **k: DeliveryResult(error="unreachable")  # type: ignore[method-assign]
    r = mesh.send("receiver", "goodbye", reply="end", msg_id="end-attempt-1")
    assert r.error == "unreachable"

    # Nothing was written: the pair stays open and THIS message's close
    # anchor does not exist — a close must never outlive its message.
    assert open_threads.live_pair("sender", "receiver", vault_path=tmp_path / "vault")
    assert not is_closed("end-attempt-1", vault_path=tmp_path / "vault")

    # The retry succeeds and closes.
    mesh.delivery.send = lambda *a, **k: DeliveryResult(delivery_id="end-1")  # type: ignore[method-assign]
    r2 = mesh.send("receiver", "goodbye", reply="end", msg_id="end-msg-1")
    assert r2.error is None
    assert is_closed("end-msg-1", vault_path=tmp_path / "vault")
    assert open_threads.live_pair("sender", "receiver", vault_path=tmp_path / "vault") is None


# --- N1 / N5 / N10 / §6: surfacing, traffic exemption, meter, inspection ---


def test_n1_thread_id_surfaced_on_envelope(tmp_path: Path) -> None:
    mesh = _mesh(tmp_path)
    private, public = generate_keypair()
    _register_peer(mesh, "alice", public)
    h, b = _signed_request(
        sender_private=private,
        sender="alice",
        recipient="receiver",
        msg_id="m-1",
    )
    env = mesh.verify_request(h, b)
    assert getattr(env, "thread_id", None)
    row = open_threads.live_pair("alice", "receiver", vault_path=tmp_path / "vault")
    assert env.thread_id == row["thread_id"]


def test_gate_runs_before_replay_marks(tmp_path: Path) -> None:
    """A refused delivery is not marked seen — its retry surfaces 'busy',
    not 'Replay' (same ordering discipline as the THREAD_CLOSED check)."""
    mesh = _mesh(tmp_path)
    private, public = generate_keypair()
    _register_peer(mesh, "alice", public)
    _seed_lease(tmp_path, "xenia", "receiver")

    h, b = _signed_request(
        sender_private=private,
        sender="alice",
        recipient="receiver",
        msg_id="busy-1",
    )
    with pytest.raises(GateRefusal):
        mesh.verify_request(h, b)
    assert not mesh.replay.has("busy-1")
    with pytest.raises(GateRefusal) as exc:
        mesh.verify_request(h, b)
    assert exc.value.detail["reason"] == "busy"
    assert exc.value.status_code == 503


def test_n10_meter_line_has_all_five_fields(tmp_path: Path, caplog) -> None:
    mesh = _mesh(tmp_path, name="sender")
    _register_peer(mesh, "receiver", url="http://127.0.0.1:9/mesh/receive")
    mesh.delivery.send = lambda *a, **k: DeliveryResult(delivery_id="x")  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="diploid_mesh.core"):
        mesh.send("receiver", "hi", reply="yes")
    lines = [r.getMessage() for r in caplog.records if "[mesh] send" in r.getMessage()]
    assert lines, "expected a meter line"
    line = lines[-1]
    for field in ("agent=receiver", "action=do", "reply=yes", "outcome=delivered", "thread="):
        assert field in line, field


def test_n10_meter_marks_refusal(tmp_path: Path, caplog) -> None:
    mesh = _mesh(tmp_path, name="sender")
    _register_peer(mesh, "receiver", url="http://127.0.0.1:9/mesh/receive")
    mesh.delivery.send = lambda *a, **k: DeliveryResult(error="busy")  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="diploid_mesh.core"):
        mesh.send("receiver", "hi", reply="yes")
    assert "outcome=refused" in caplog.records[-1].getMessage()


def test_section6_rows_and_live_reported_separately(tmp_path: Path) -> None:
    _seed_lease(tmp_path, "xenia", "receiver", live=False)  # expired, not swept
    _seed_lease(tmp_path, "alice", "receiver")
    s = open_threads.summary(vault_path=tmp_path / "vault")
    assert s == {"rows": 2, "live": 1}
