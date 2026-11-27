"""Append-only local mesh conversation history journal."""

from __future__ import annotations

import errno
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from diploid_mesh._paths import safe_dir_name

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only in production
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows-only feature
    msvcrt = None  # type: ignore[assignment]


_LOCK_ACQUIRE_DEADLINE_S = 60.0
_RETRYABLE_LOCK_ERRNOS = (errno.EACCES, errno.EDEADLK)


def _lock_fd_windows(fd: int) -> None:  # pragma: no cover - Windows-only feature
    os.lseek(fd, 0, os.SEEK_SET)
    deadline = time.monotonic() + _LOCK_ACQUIRE_DEADLINE_S
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in _RETRYABLE_LOCK_ERRNOS or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _unlock_fd_windows(fd: int) -> None:  # pragma: no cover - Windows-only feature
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


HISTORY_FILENAME = "chat_mesh_history.jsonl"
SCHEMA_VERSION = 1

REQUIRED_FIELDS = {
    "schema_version",
    "kind",
    "origin",
    "msg_id",
    "ref",
    "lease_thread_id",
    "wire_thread",
    "sender",
    "recipient",
    "direction",
    "action",
    "reply",
    "session",
    "from_session",
    "body",
    "body_chars",
    "chat_id",
    "event_ts",
    "recorded_at",
    "delivery_state",
    "delivery_id",
    "delivery_error",
    "delivery_detail",
}

VALID_KINDS = {"message", "dsn"}
VALID_ORIGINS = {"live", "legacy-seed"}
VALID_DIRECTIONS = {"inbound", "outbound"}
VALID_ACTIONS = {"do", "info"}
VALID_REPLIES = {"yes", "no", "end"}
VALID_DELIVERY_STATES = {
    "inbound-wake-enqueued",
    "inbound-digest-queued",
    "inbound-digest-merged",
    "inbound-dsn-recorded",
    "outbound-recipient-accepted",
    "outbound-recipient-refused",
    "outbound-local-refused",
    "outbound-send-failed",
}


def history_path(sessions_root: str | Path, chat_id: str) -> Path:
    return Path(sessions_root) / safe_dir_name(chat_id) / HISTORY_FILENAME


def validate_event(event: dict[str, Any]) -> None:
    missing = REQUIRED_FIELDS - event.keys()
    if missing:
        raise ValueError(f"mesh history event missing fields: {sorted(missing)}")
    unknown = event.keys() - REQUIRED_FIELDS
    if unknown:
        raise ValueError(f"mesh history event has unknown v1 fields: {sorted(unknown)}")
    if event["schema_version"] != SCHEMA_VERSION or isinstance(event["schema_version"], bool):
        raise ValueError(f"unsupported mesh history schema_version: {event['schema_version']}")
    if event["kind"] not in VALID_KINDS:
        raise ValueError(f"invalid mesh history kind: {event['kind']}")
    if event["origin"] not in VALID_ORIGINS:
        raise ValueError(f"invalid mesh history origin: {event['origin']}")
    if event["direction"] not in VALID_DIRECTIONS:
        raise ValueError(f"invalid mesh history direction: {event['direction']}")
    if event["action"] not in VALID_ACTIONS:
        raise ValueError(f"invalid mesh history action: {event['action']}")
    if event["reply"] not in VALID_REPLIES:
        raise ValueError(f"invalid mesh history reply: {event['reply']}")
    if event["delivery_state"] not in VALID_DELIVERY_STATES:
        raise ValueError(f"invalid mesh history delivery_state: {event['delivery_state']}")
    string_fields = (
        "kind",
        "origin",
        "msg_id",
        "sender",
        "recipient",
        "direction",
        "action",
        "reply",
        "body",
        "chat_id",
        "delivery_state",
    )
    for field in string_fields:
        if not isinstance(event[field], str):
            raise TypeError(f"mesh history {field} must be a string")
    nullable_string_fields = (
        "ref",
        "lease_thread_id",
        "wire_thread",
        "session",
        "from_session",
        "delivery_id",
        "delivery_error",
    )
    for field in nullable_string_fields:
        if event[field] is not None and not isinstance(event[field], str):
            raise TypeError(f"mesh history {field} must be a string or null")
    if not isinstance(event["body_chars"], int) or isinstance(event["body_chars"], bool):
        raise TypeError("mesh history body_chars must be an integer")
    for field in ("event_ts", "recorded_at"):
        if not isinstance(event[field], (int, float)) or isinstance(event[field], bool):
            raise TypeError(f"mesh history {field} must be numeric")
    if event["delivery_detail"] is not None and not isinstance(event["delivery_detail"], dict):
        raise TypeError("mesh history delivery_detail must be an object or null")
    if event["body_chars"] != len(event["body"]):
        raise ValueError("mesh history body_chars does not match body length")
    if event["direction"] == "inbound" and not event["delivery_state"].startswith("inbound-"):
        raise ValueError("inbound event must use an inbound delivery_state")
    if event["direction"] == "outbound" and not event["delivery_state"].startswith("outbound-"):
        raise ValueError("outbound event must use an outbound delivery_state")


def append_event(sessions_root: str | Path, chat_id: str, event: dict[str, Any]) -> Path:
    """Validate and append one immutable event to the chat-local JSONL journal."""
    validate_event(event)
    path = history_path(sessions_root, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:
            _lock_fd_windows(fd)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            path.chmod(0o600)
            lock_path.chmod(0o600)
        except OSError:
            pass
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            try:
                _unlock_fd_windows(fd)
            except OSError:
                pass
        os.close(fd)
    return path


def make_event(
    *,
    kind: str,
    msg_id: str,
    ref: str | None,
    lease_thread_id: str | None,
    wire_thread: str | None,
    sender: str,
    recipient: str,
    direction: str,
    action: str,
    reply: str,
    session: str | None,
    from_session: str | None,
    body: str,
    chat_id: str,
    event_ts: float,
    delivery_state: str,
    delivery_id: str | None = None,
    delivery_error: str | None = None,
    delivery_detail: dict[str, Any] | None = None,
    origin: str = "live",
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "origin": origin,
        "msg_id": msg_id,
        "ref": ref,
        "lease_thread_id": lease_thread_id,
        "wire_thread": wire_thread,
        "sender": sender,
        "recipient": recipient,
        "direction": direction,
        "action": action,
        "reply": reply,
        "session": session,
        "from_session": from_session,
        "body": body,
        "body_chars": len(body),
        "chat_id": chat_id,
        "event_ts": event_ts,
        "recorded_at": time.time(),
        "delivery_state": delivery_state,
        "delivery_id": delivery_id,
        "delivery_error": delivery_error,
        "delivery_detail": delivery_detail,
    }


def iter_events(sessions_root: str | Path) -> Iterator[dict[str, Any]]:
    """Yield parseable events across all chat journals under one seat."""
    root = Path(sessions_root)
    if not root.is_dir():
        return
    for path in sorted(root.glob(f"*/{HISTORY_FILENAME}")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def count_exact_related_events(
    sessions_root: str | Path,
    key: str | None,
    *,
    exclude_msg_id: str | None = None,
) -> int:
    """Count exact indexed-key matches as a lower-bound prompt-depth hint.

    This deliberately is not the Lane B resolver: it follows no parent-chain
    closure and therefore may undercount. It never overstates absence as proof.
    """
    if not key:
        return 0
    count = 0
    for event in iter_events(sessions_root):
        if exclude_msg_id and event.get("msg_id") == exclude_msg_id:
            continue
        keys = {
            event.get("msg_id"),
            event.get("ref"),
            event.get("lease_thread_id"),
            event.get("wire_thread"),
        }
        if key in keys:
            count += 1
    return count
