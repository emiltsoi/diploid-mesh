"""FastAPI ingress handler for mesh webhooks."""

from __future__ import annotations

import logging
from typing import Any

from diploid_agent.transport.ingress import IngressHandler
from fastapi import Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from diploid_mesh.config import DiploidMeshConfig
from diploid_mesh.core import DiploidMesh

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
        except ValueError as exc:
            logger.warning("[diploid-mesh] rejected inbound: %s", exc)
            return JSONResponse({"status": "rejected", "reason": str(exc)}, status_code=400)
        except Exception:
            logger.exception("[diploid-mesh] error verifying inbound")
            return JSONResponse({"status": "error", "reason": "internal error"}, status_code=500)

        chat_id = self.mesh.resolve_chat_id(envelope.sender)
        display_text = self._display_text(envelope)

        from diploid_agent.models import WakeEvent

        event_id = f"mesh:{envelope.msg_id}"
        event = WakeEvent(
            id=event_id,
            chat_id=chat_id,
            reason="mesh",
            priority=1,
            scheduled_at=__import__("time").time(),
            payload={
                "user_message": display_text,
                "mesh": {
                    "sender": envelope.sender,
                    "recipient": envelope.recipient,
                    "action": envelope.action,
                    "reply": envelope.reply,
                    "ref": envelope.ref,
                    "message_id": envelope.msg_id,
                },
            },
            silent=False,
            ready=True,
        )
        self.runtime.wake_queue.enqueue(event)

        try:
            result = await run_in_threadpool(
                self.runtime.wake,
                chat_id,
                event_id=event_id,
            )
        except Exception:
            logger.exception("[diploid-mesh] failed to wake chat %s", chat_id)
            return JSONResponse(
                {"status": "error", "reason": "failed to wake chat"}, status_code=500
            )

        if result.reply == "Chat is busy; wake re-enqueued.":
            return JSONResponse(
                {"status": "queued", "delivery_id": envelope.msg_id},
                status_code=202,
            )

        return JSONResponse(
            {"status": "accepted", "delivery_id": envelope.msg_id, "chat_id": chat_id},
            status_code=202,
        )

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
