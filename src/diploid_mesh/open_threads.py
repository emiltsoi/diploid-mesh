"""Open-thread lease registry — the diploid thread gate's claim store.

Port of the hermes_mesh lease model per
``fleet/shared/specs/mesh-thread-gate-port-spec.md`` (ruling §11). The
fleet has no shared registry directory, so each seat's registry lives
under its own vault and the serialized decision point is the recipient's
ingress — the POST is the claim. The registry is a list of entries (not a
mapping) safe under concurrent access from multiple processes.

Data model (spec §2.1)::

    {
      "thread_id": "uuid", "opened_by": "wife", "peer": "wife",
      "opened_at": t, "lease_until": t, "lease_minutes": 30, "replies": 0
    }

Expired rows are never swept (spec §6); liveness is computed at read.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only feature
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows-only feature
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()
_DEFAULT_NAME = "open-threads.json"
_DEFAULT_LEASE_MINUTES = 30


def _registry_path(vault_path: str | Path | None = None) -> Path:
    env = os.getenv("MESH_OPEN_THREADS")
    if env:
        return Path(env)
    if vault_path:
        root = Path(vault_path).expanduser().resolve()
    else:
        env = os.getenv("MESH_VAULT_PATH")
        root = Path(env) if env else Path.home() / ".mesh"
    return root / "mesh" / _DEFAULT_NAME


_LOCK_ACQUIRE_DEADLINE_S = 60.0


def _lock_fd_windows(fd: int) -> None:
    """Blocking byte-range lock via msvcrt. LK_LOCK gives up after ~10s, so
    retry until the deadline — flock(LOCK_EX) waits indefinitely and the
    decision point must not silently proceed unlocked."""
    os.lseek(fd, 0, os.SEEK_SET)
    deadline = time.monotonic() + _LOCK_ACQUIRE_DEADLINE_S
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _unlock_fd_windows(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def _registry_lock(path: Path):
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    with _WRITE_LOCK:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            elif msvcrt is not None:
                _lock_fd_windows(fd)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                try:
                    _unlock_fd_windows(fd)
                except OSError:
                    pass
            os.close(fd)


def _read_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("[mesh] open-threads registry unreadable: %s", path)
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _write_entries(entries: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="open-threads-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:  # pragma: no cover - dir fsync unsupported on this FS
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _lease_minutes() -> int:
    try:
        return int(os.getenv("MESH_LEASE_MINUTES", _DEFAULT_LEASE_MINUTES))
    except (TypeError, ValueError):
        return _DEFAULT_LEASE_MINUTES


def _live(entry: dict, now: float) -> bool:
    return float(entry.get("lease_until", 0)) > now


def _is_pair(entry: dict, a: str, b: str) -> bool:
    return {entry.get("opened_by"), entry.get("peer")} == {a, b}


@dataclass
class GateOutcome:
    """Result of the atomic inbound gate decision."""

    action: str  # "deliver" | "refuse"
    entry: dict | None = None  # the lease row for a delivery
    refusal: dict | None = None  # typed refusal body (N6) for a refusal
    created: bool = False  # the delivered row was minted by this request


def _new_lease(opened_by: str, peer: str, now: float, lease_minutes: int) -> dict:
    return {
        "thread_id": str(uuid.uuid4()),
        "opened_by": opened_by,
        "peer": peer,
        "opened_at": now,
        "lease_until": now + lease_minutes * 60,
        "lease_minutes": lease_minutes,
        "replies": 0,
    }


def _busy_refusal(busy_row: dict, peer: str) -> GateOutcome:
    """N6 typed refusal: the busy lease's own fields, plus a reroute address.

    For a busy predicament the only honest destination is temporal — the
    lease's expiry is the address (spec §4 N6: a time is a valid address).
    """
    return GateOutcome(
        "refuse",
        refusal={
            "reason": "busy",
            "thread_id": busy_row.get("thread_id"),
            "peer": peer,
            "opened_by": busy_row.get("opened_by"),
            "opened_at": busy_row.get("opened_at"),
            "lease_until": busy_row.get("lease_until"),
            "retry_after": busy_row.get("lease_until"),
            "reroute": {"retry_after": busy_row.get("lease_until")},
        },
    )


def inbound_gate(
    *,
    sender: str,
    peer: str,
    reply: str,
    vault_path: str | Path | None = None,
    now: float | None = None,
) -> GateOutcome:
    """Atomic check-and-claim for an inbound message (N3, distributed shape).

    Runs entirely under the registry lock: pair match (N5 thread traffic),
    then the normative busy predicate (spec §3 — a lease where `peer` is us
    and `opened_by` is somebody other than the sender), then the claim.
    Notices (N9) and terminators (N7) are exempt from the gate.
    """
    now = time.time() if now is None else now
    path = _registry_path(vault_path)
    with _registry_lock(path):
        entries = _read_entries(path)

        if reply == "no":
            # N9 — a notice carries no reply debt; never claimed, never refused.
            return GateOutcome("deliver")

        pair = next((e for e in entries if _is_pair(e, sender, peer) and _live(e, now)), None)

        if reply == "end":
            # N7 — exempt from the busy gate. A provable duplicate close
            # (ref -> already-closed anchor) is refused earlier at the
            # THREAD_CLOSED check; a terminator we cannot prove redundant
            # must not be refused — a blocked goodbye is the §8.2 defect.
            return GateOutcome("deliver", entry=pair)

        if pair is not None:
            # N2/N5 — thread traffic: refresh expiry, reuse the thread_id,
            # never mint a second one for the pair.
            pair["lease_until"] = now + float(pair.get("lease_minutes", 30)) * 60
            pair["replies"] = int(pair.get("replies", 0)) + 1
            _write_entries(entries, path)
            return GateOutcome("deliver", entry=pair)

        busy = next(
            (
                e
                for e in entries
                if _live(e, now) and e.get("peer") == peer and e.get("opened_by") != sender
            ),
            None,
        )
        if busy is not None:
            return _busy_refusal(busy, peer)

        entry = _new_lease(sender, peer, now, _lease_minutes())
        entries.append(entry)
        _write_entries(entries, path)
        return GateOutcome("deliver", entry=entry, created=True)


def claim_outbound(
    *,
    me: str,
    peer: str,
    vault_path: str | Path | None = None,
    now: float | None = None,
) -> GateOutcome:
    """Sender-side claim (N3): merge with a live pair row, refuse on busy,
    or create one.

    The §3 busy predicate runs here too, not only at inbound_gate — under
    a shared registry file the claim is the contested act (spec §11): a
    pre-minted pair row would pair-match the sender's own envelope at the
    receiver's gate, making the wire busy-check a dead letter. Order
    mirrors inbound_gate — pair first (an opener is never self-busy),
    then busy on a live row where ``peer`` is the recipient and
    ``opened_by`` is somebody else. ``GateOutcome.created`` tells the
    caller whether the row was minted by this send — only a created claim
    may be released when the transport gives up (N4); a merged row
    predates this send. In a per-seat registry the busy check is a no-op:
    the only ``peer == target`` rows a sender's file can hold are her own
    pair rows.
    """
    now = time.time() if now is None else now
    path = _registry_path(vault_path)
    with _registry_lock(path):
        entries = _read_entries(path)
        existing = next((e for e in entries if _is_pair(e, me, peer) and _live(e, now)), None)
        if existing is not None:
            existing["lease_until"] = now + float(existing.get("lease_minutes", 30)) * 60
            existing["replies"] = int(existing.get("replies", 0)) + 1
            _write_entries(entries, path)
            return GateOutcome("deliver", entry=existing)
        busy = next(
            (
                e
                for e in entries
                if _live(e, now) and e.get("peer") == peer and e.get("opened_by") != me
            ),
            None,
        )
        if busy is not None:
            return _busy_refusal(busy, peer)
        entry = _new_lease(me, peer, now, _lease_minutes())
        entries.append(entry)
        _write_entries(entries, path)
        return GateOutcome("deliver", entry=entry, created=True)


def release_pair(a: str, b: str, *, vault_path: str | Path | None = None) -> int:
    """Remove every row for the pair — on close, or on release of a
    freshly-created claim whose send failed (N4). Returns rows removed."""
    path = _registry_path(vault_path)
    with _registry_lock(path):
        entries = _read_entries(path)
        kept = [e for e in entries if not _is_pair(e, a, b)]
        removed = len(entries) - len(kept)
        if removed:
            _write_entries(kept, path)
    return removed


def live_pair(
    a: str, b: str, *, vault_path: str | Path | None = None, now: float | None = None
) -> dict | None:
    """Return the live lease row for the pair, if any (N1 thread surfacing)."""
    now = time.time() if now is None else now
    path = _registry_path(vault_path)
    with _registry_lock(path):
        entries = _read_entries(path)
    return next((e for e in entries if _is_pair(e, a, b) and _live(e, now)), None)


def summary(*, vault_path: str | Path | None = None, now: float | None = None) -> dict:
    """Inspection surface (spec §6): rows and live reported separately."""
    now = time.time() if now is None else now
    path = _registry_path(vault_path)
    with _registry_lock(path):
        entries = _read_entries(path)
    return {
        "rows": len(entries),
        "live": sum(1 for e in entries if _live(e, now)),
    }
