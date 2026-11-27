"""Read-side reconstruction and paging for local mesh conversation history."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from diploid_mesh.history import iter_events

INDEXED_KEY_FIELDS = ("msg_id", "ref", "lease_thread_id", "wire_thread")
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
DEFAULT_MAX_CHARS = 12000
MAX_CHARS = 100000
MAX_COMPONENT_EVENTS = 200
MAX_PARENT_HOPS = 32


def _sort_key(event: dict[str, Any]) -> tuple[float, float, str, str]:
    def number(value: Any) -> float:
        return (
            float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        )

    return (
        number(event.get("event_ts")),
        number(event.get("recorded_at")),
        str(event.get("chat_id") or ""),
        str(event.get("msg_id") or ""),
    )


def _body_chars(event: dict[str, Any]) -> int:
    value = event.get("body_chars")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return len(str(event.get("body") or ""))


def _event_hash(event: dict[str, Any]) -> str:
    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _encode_cursor(ref: str, event: dict[str, Any]) -> str:
    payload = {"v": 1, "ref": ref, "event_hash": _event_hash(event)}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(ref: str, cursor: str) -> str:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid mesh_thread before cursor") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("ref") != ref:
        raise ValueError("mesh_thread before cursor does not match this ref")
    event_hash = payload.get("event_hash")
    if not isinstance(event_hash, str) or not event_hash:
        raise ValueError("invalid mesh_thread before cursor")
    return event_hash


def _indexed_exact_fields(event: dict[str, Any], key: str) -> list[str]:
    return [field for field in INDEXED_KEY_FIELDS if event.get(field) == key]


def _resolution_class(exact_fields: list[str], *, key_is_message_id: bool) -> str:
    if not exact_fields:
        return "inferred-parent"
    if "ref" in exact_fields and not key_is_message_id:
        return "exact-slug"
    return "exact-id"


def resolve_thread(
    sessions_root: str | Path,
    key: str,
    *,
    max_events: int = MAX_COMPONENT_EVENTS,
    max_hops: int = MAX_PARENT_HOPS,
) -> dict[str, Any]:
    """Resolve one honest bounded connected set across all local chat journals.

    Seed events are exact matches on indexed keys. Closure then follows the
    parent/child connected component formed when one event's ``ref`` equals
    another event's ``msg_id``. The closure is deliberately bounded; truncation
    is metadata, never silent completeness.
    """
    if not isinstance(key, str):
        raise TypeError("mesh_thread ref must be a string")
    if not key:
        raise ValueError("mesh_thread ref must be a non-empty string")

    all_events = [event for event in iter_events(sessions_root) if isinstance(event, dict)]
    order = sorted(range(len(all_events)), key=lambda index: _sort_key(all_events[index]))

    by_msg_id: dict[str, list[int]] = defaultdict(list)
    for index, event in enumerate(all_events):
        msg_id = event.get("msg_id")
        if isinstance(msg_id, str) and msg_id:
            by_msg_id[msg_id].append(index)

    adjacency: dict[int, set[int]] = defaultdict(set)
    for index, event in enumerate(all_events):
        parent = event.get("ref")
        if not isinstance(parent, str) or not parent:
            continue
        for parent_index in by_msg_id.get(parent, []):
            if parent_index == index:
                continue
            adjacency[index].add(parent_index)
            adjacency[parent_index].add(index)

    seed_indices = {
        index
        for index, event in enumerate(all_events)
        if key in {event.get(field) for field in INDEXED_KEY_FIELDS}
    }
    if not seed_indices:
        return {
            "events": [],
            "seed_matches": 0,
            "truncated": False,
            "max_events": max_events,
            "max_hops": max_hops,
            "journal_entries_scanned": len(all_events),
            "key_is_message_id": key in by_msg_id,
        }

    selected: set[int] = set()
    truncated = False
    # When the bounded component cannot hold every exact seed, prefer the
    # newest evidence: read output is explicitly tail-biased.
    initial = [index for index in reversed(order) if index in seed_indices]
    for index in initial:
        if len(selected) >= max_events:
            truncated = True
            break
        selected.add(index)

    frontier = [index for index in initial if index in selected]
    hops = 0
    while frontier:
        if hops >= max_hops:
            if any(adjacency[index] - selected for index in frontier):
                truncated = True
            break
        next_frontier: list[int] = []
        for index in frontier:
            for neighbor in sorted(
                adjacency[index], key=lambda item: _sort_key(all_events[item]), reverse=True
            ):
                if neighbor in selected:
                    continue
                if len(selected) >= max_events:
                    truncated = True
                    continue
                selected.add(neighbor)
                next_frontier.append(neighbor)
        frontier = next_frontier
        hops += 1

    selected_events = [all_events[index] for index in order if index in selected]
    return {
        "events": selected_events,
        "seed_matches": len(seed_indices),
        "truncated": truncated,
        "max_events": max_events,
        "max_hops": max_hops,
        "journal_entries_scanned": len(all_events),
        "key_is_message_id": key in by_msg_id,
    }


def _validate_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"mesh_thread {name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"mesh_thread {name} must be between {minimum} and {maximum}")
    return value


def read_thread_page(
    sessions_root: str | Path,
    key: str,
    *,
    limit: int = DEFAULT_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
    before: str | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a newest-first, tail-biased page of full thread bodies.

    ``before`` is the opaque older-page cursor emitted as ``next_before``.
    ``offset`` is a simple additional newest-first skip for clients that prefer
    index paging; cursor paging is more stable under journal appends.
    """
    limit = _validate_int("limit", limit, minimum=1, maximum=MAX_LIMIT)
    max_chars = _validate_int("max_chars", max_chars, minimum=1, maximum=MAX_CHARS)
    offset = _validate_int("offset", offset, minimum=0, maximum=100000)

    resolution = resolve_thread(sessions_root, key)
    key_is_message_id = resolution["key_is_message_id"]
    ascending = resolution["events"]
    newest_first = list(reversed(ascending))

    start = 0
    if before:
        wanted_hash = _decode_cursor(key, before)
        hashes = [_event_hash(event) for event in newest_first]
        try:
            start = hashes.index(wanted_hash) + 1
        except ValueError as exc:
            raise ValueError("mesh_thread before cursor is not in the current resolution") from exc
    start += offset

    candidates = newest_first[start:]
    newer_events = newest_first[:start]
    selected: list[dict[str, Any]] = []
    returned_chars = 0
    single_event_exceeds_budget = False

    for event in candidates:
        if len(selected) >= limit:
            break
        chars = _body_chars(event)
        if selected and returned_chars + chars > max_chars:
            break
        selected.append(event)
        returned_chars += chars
        if len(selected) == 1 and chars > max_chars:
            # A body is indivisible under the full-body contract. Return the
            # newest oversize body once rather than making the thread unreadable.
            single_event_exceeds_budget = True
            break

    older_events = candidates[len(selected) :]
    older_cursor = None
    if older_events and selected:
        older_cursor = _encode_cursor(key, selected[-1])

    entries = []
    for event in selected:
        exact_fields = _indexed_exact_fields(event, key)
        entries.append(
            {
                "msg_id": event.get("msg_id"),
                "direction": event.get("direction"),
                "event_ts": event.get("event_ts"),
                "sender": event.get("sender"),
                "recipient": event.get("recipient"),
                "session": event.get("session"),
                "from_session": event.get("from_session"),
                "chat_id": event.get("chat_id"),
                "kind": event.get("kind"),
                "action": event.get("action"),
                "reply": event.get("reply"),
                "ref": event.get("ref"),
                "lease_thread_id": event.get("lease_thread_id"),
                "wire_thread": event.get("wire_thread"),
                "delivery_state": event.get("delivery_state"),
                "delivery_error": event.get("delivery_error"),
                "resolution": _resolution_class(exact_fields, key_is_message_id=key_is_message_id),
                "matched_fields": exact_fields,
                "body_chars": _body_chars(event),
                "body": event.get("body") or "",
            }
        )

    total_chars = sum(_body_chars(event) for event in ascending)
    older_chars = sum(_body_chars(event) for event in older_events)
    newer_chars = sum(_body_chars(event) for event in newer_events)
    return {
        "schema_version": 1,
        "ref": key,
        "resolution": {
            "seed_matches": resolution["seed_matches"],
            "key_is_message_id": key_is_message_id,
            "resolved_entries": len(ascending),
            "truncated": resolution["truncated"],
            "max_events": resolution["max_events"],
            "max_hops": resolution["max_hops"],
            "journal_entries_scanned": resolution["journal_entries_scanned"],
        },
        "page": {
            "ordering": "newest-first",
            "tail_biased": True,
            "limit": limit,
            "max_chars": max_chars,
            "offset": offset,
            "returned_entries": len(entries),
            "returned_chars": returned_chars,
            "total_entries": len(ascending),
            "total_chars": total_chars,
            "elided_entries": len(older_events),
            "elided_chars": older_chars,
            "has_older": bool(older_events),
            "next_before": older_cursor,
            "newer_entries": len(newer_events),
            "newer_chars": newer_chars,
            "single_event_exceeds_budget": single_event_exceeds_budget,
        },
        "events": entries,
    }
