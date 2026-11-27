"""FastAPI ingress handler for mesh webhooks."""

from __future__ import annotations

import logging
import time
from typing import Any

from diploid_agent.transport.ingress import IngressHandler
from fastapi import Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from diploid_mesh import open_threads
from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.core import DiploidMesh, GateRefusal, ReplaySeenError
from diploid_mesh.history import append_event, make_event

logger = logging.getLogger(__name__)


class DiploidMeshIngress(IngressHandler):
    """Receives signed mesh webhooks and wakes the diploid runtime."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.config = DiploidMeshConfig(runtime.config.harness.mesh)
        self.mesh = DiploidMesh(self.config)

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}

        try:
            envelope = await run_in_threadpool(self.mesh.verify_request, headers, body)
        except ReplaySeenError as exc:
            # Idempotent accept: replay.add only runs after every rejectable
            # check passes, so "already seen" proves the envelope was accepted
            # before. A 400 here makes senders hear failure for a delivered
            # letter whenever a response is lost mid-flight.
            logger.info("[diploid-mesh] duplicate delivery: %s", exc.msg_id)
            return JSONResponse(
                {"status": "duplicate", "delivery_id": exc.msg_id},
                status_code=202,
            )
        except GateRefusal as exc:
            # Typed refusal (N6) — the body IS the refusal: reason, the
            # blocking lease's fields, and the reroute address. Refusals
            # never reach the replay cache or the wake queue.
            logger.info("[diploid-mesh] gate refusal: %s", exc.detail)
            return JSONResponse({"status": "refused", **exc.detail}, status_code=exc.status_code)
        except ValueError as exc:
            logger.warning("[diploid-mesh] rejected inbound: %s", exc)
            return JSONResponse({"status": "rejected", "reason": str(exc)}, status_code=400)
        except Exception:
            logger.exception("[diploid-mesh] error verifying inbound")
            return JSONResponse({"status": "error", "reason": "internal error"}, status_code=500)

        observed_at = time.time()
        chat_id = self.mesh.resolve_chat_id(envelope.sender, envelope.session)
        display_text = self._display_text(envelope)

        is_dsn = (envelope.body or "").startswith("[mesh-dsn]") or self._is_dsn_header(headers)

        mesh_payload = {
            "sender": envelope.sender,
            "recipient": envelope.recipient,
            "action": envelope.action,
            "reply": envelope.reply,
            "ref": envelope.ref,
            "message_id": envelope.msg_id,
            "thread_id": getattr(envelope, "thread_id", None),
            "session": envelope.session,
            "from_session": envelope.from_session,
            "body": envelope.body or "",
        }

        # DSNs are terminal: record without a model turn.
        if is_dsn:
            try:
                await run_in_threadpool(
                    self.runtime.record_mesh_message,
                    chat_id,
                    display_text,
                    mesh_payload,
                )
            except Exception:
                logger.exception("[diploid-mesh] failed to record DSN")
                return JSONResponse(
                    {"status": "error", "reason": "failed to record DSN"},
                    status_code=500,
                )
            self._journal_inbound(
                mesh_payload,
                chat_id,
                delivery_state="inbound-dsn-recorded",
                event_ts=observed_at,
                is_dsn=True,
            )
            return JSONResponse(
                {"status": "accepted", "delivery_id": envelope.msg_id, "chat_id": chat_id},
                status_code=202,
            )

        # reply=yes, reply=no, and reply=end all wake a real ACP turn.
        # The MCP server enforces different send budgets based on current_mesh.reply.
        from diploid_agent.models import WakeEvent

        coalesce = getattr(self.runtime.config.harness.mesh, "coalesce", None)
        coalescible = (
            coalesce is not None
            and coalesce.enabled
            and envelope.action == "info"
            and envelope.reply == "no"
        )
        if coalescible:
            # Fold into the peer's armed digest — pending IS the queue file.
            # key_field/list_field mirror the merge kwargs. The digest id is
            # msg_id-unique on purpose: a deterministic id would let enqueue
            # overwrite a leased (in-flight) digest and complete() would eat
            # the parked message unseen — agent's leg-1 finding.
            digest_id = f"mesh-digest:{chat_id}:{envelope.sender}:{envelope.msg_id}"
            event = WakeEvent(
                id=digest_id,
                chat_id=chat_id,
                reason="mesh_digest",
                priority=1,
                scheduled_at=time.time() + coalesce.window_seconds,
                payload={
                    "sender": envelope.sender,
                    "messages": [{"display": display_text, "mesh": mesh_payload}],
                    "silent": False,
                },
                silent=False,
                ready=True,
            )
            try:
                merged = self.runtime.wake_queue.merge_into_ready(
                    event,
                    key=envelope.sender,
                    key_field="sender",
                    list_field="messages",
                )
                if merged is None:
                    self.runtime.wake_queue.enqueue(event)
                elif len(merged.payload.get("messages") or []) >= coalesce.max_items:
                    # merged.id — the absorbing digest's own id, not this
                    # envelope's (they differ whenever a merge absorbs).
                    self.runtime.wake_queue.fire_now(merged.id)
            except Exception:
                if getattr(envelope, "gate_created", False):
                    open_threads.release_pair(
                        envelope.sender,
                        envelope.recipient,
                        vault_path=self.mesh.core_config.vault_path,
                    )
                self.mesh.replay.discard(envelope.msg_id)
                logger.exception("[diploid-mesh] wake enqueue failed; lease released")
                return JSONResponse(
                    {"status": "error", "reason": "wake enqueue failed"},
                    status_code=500,
                )
            self._journal_inbound(
                mesh_payload,
                chat_id,
                delivery_state=(
                    "inbound-digest-merged" if merged is not None else "inbound-digest-queued"
                ),
                event_ts=observed_at,
                is_dsn=False,
            )
            self.mesh.commit_terminator(envelope)
            return JSONResponse(
                {"status": "accepted", "delivery_id": envelope.msg_id, "chat_id": chat_id},
                status_code=202,
            )

        event_id = f"mesh:{envelope.msg_id}"
        event = WakeEvent(
            id=event_id,
            chat_id=chat_id,
            reason="mesh",
            priority=1,
            scheduled_at=time.time(),
            payload={
                "user_message": display_text,
                "mesh": mesh_payload,
                # Surface mesh wakes as user-visible turns even if the waker
                # has to retry the wake later (it falls back to payload.silent).
                "silent": False,
            },
            silent=False,
            ready=True,
        )
        try:
            self.runtime.wake_queue.enqueue(event)
        except Exception:
            # Give-up (N4): a claim minted by THIS delivery is dropped so
            # the slot is not leaked by a wake we could not start, and the
            # replay mark is discarded so the sender's retry verifies fresh
            # instead of being swallowed as a duplicate. A merged lease
            # predates the request and stays.
            if getattr(envelope, "gate_created", False):
                open_threads.release_pair(
                    envelope.sender,
                    envelope.recipient,
                    vault_path=self.mesh.core_config.vault_path,
                )
            self.mesh.replay.discard(envelope.msg_id)
            logger.exception("[diploid-mesh] wake enqueue failed; lease released")
            return JSONResponse(
                {"status": "error", "reason": "wake enqueue failed"},
                status_code=500,
            )

        self._journal_inbound(
            mesh_payload,
            chat_id,
            delivery_state="inbound-wake-enqueued",
            event_ts=observed_at,
            is_dsn=False,
        )

        # The terminator's close lands only once its wake exists — a close
        # must never outlive the message carrying it (N8, receive side).
        self.mesh.commit_terminator(envelope)

        # Flush-on-priority: this delivery was urgent enough to land now, so
        # the peer's parked digest fires on the next tick rather than waiting
        # out its window. Scan-based: digest ids are msg_id-suffixed, so an
        # exact id can't be constructed here. No-op when none is armed.
        if coalesce is not None and coalesce.enabled:
            self.runtime.wake_queue.fire_matching(
                chat_id=chat_id,
                reason="mesh_digest",
                key_field="sender",
                key=envelope.sender,
            )

        # Return 202 immediately. The TimerService (or any waker subscribed to
        # the event bus) will pop the due wake and run it on its own single
        # thread. This keeps the HTTP response fast without dragging the ACP
        # client into Starlette's worker-thread pool, which can race on the
        # asyncio event loop.
        return JSONResponse(
            {"status": "accepted", "delivery_id": envelope.msg_id, "chat_id": chat_id},
            status_code=202,
        )

    def _journal_inbound(
        self,
        mesh_payload: dict[str, Any],
        chat_id: str,
        *,
        delivery_state: str,
        event_ts: float,
        is_dsn: bool,
    ) -> None:
        """Best-effort journal write; history failure must not reject delivered traffic."""
        try:
            event = make_event(
                kind="dsn" if is_dsn else "message",
                msg_id=mesh_payload["message_id"],
                ref=mesh_payload.get("ref"),
                lease_thread_id=mesh_payload.get("thread_id"),
                wire_thread=None,
                sender=mesh_payload["sender"],
                recipient=mesh_payload["recipient"],
                direction="inbound",
                action=mesh_payload["action"],
                reply=mesh_payload["reply"],
                session=mesh_payload.get("session"),
                from_session=mesh_payload.get("from_session"),
                body=mesh_payload.get("body") or "",
                chat_id=chat_id,
                event_ts=event_ts,
                delivery_state=delivery_state,
            )
            append_event(self.runtime.config.harness.sessions_root, chat_id, event)
        except Exception:
            logger.exception("[diploid-mesh] failed to journal inbound mesh event")

    @staticmethod
    def _is_dsn_header(headers: dict[str, str]) -> bool:
        value = headers.get("x-mesh-dsn", "").lower()
        return value in ("1", "true", "yes")

    def _display_text(self, envelope) -> str:
        body = envelope.body or ""
        if body.startswith("[mesh-dsn]"):
            # Surface DSNs clearly to the model.
            return f"[mesh-dsn from {envelope.sender}] {body}"
        return f"[{envelope.sender}] {body}".strip()


def create_handler(runtime: Any) -> DiploidMeshIngress:
    """Factory used by diploid-agent's ingress loader."""
    return DiploidMeshIngress(runtime)


# Expose a loader-friendly name so `diploid_mesh.ingress` can be loaded as an ingress module.
Ingress = DiploidMeshIngress
