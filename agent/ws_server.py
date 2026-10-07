"""Twilio Media Stream WebSocket handler -- a straight port of app.py's
/ws FastAPI endpoint, wrapping the raw ASGI scope/receive/send in a
starlette.websockets.WebSocket so pipecat's FastAPIWebsocketTransport (which
only needs that interface) works completely unchanged.
"""

import json

from django.utils import timezone
from starlette.websockets import WebSocket, WebSocketDisconnect

from main import load_results, run_call_twilio, save_call_outcome_if_unset

from .models import CallLog
from .state import call_claim_context, call_pending_extension, state


async def _sync_call_log(call_sid: str):
    """Snapshot call_results.json into the matching CallLog row.

    This runs every time _run_call_twilio_bg finishes -- which includes the
    mid-call disconnect caused by a DTMF-redirect reconnect, NOT just the
    true end of the call (see state.py's comment on call_claim_context).
    call_outcome is only ever written to call_results.json at a genuine
    completion point (success, ivr_dead_end_needs_callback, error, or a
    Twilio status-callback outcome) -- never during a mid-call redirect --
    so only treat the CallLog row as finished (set ended_at) when it's
    actually present. Captured fields are always safe to sync regardless.
    """
    results = load_results()
    call_outcome = results.get("call_outcome", {}).get("value", "")
    fields_captured = {k: v["value"] for k, v in results.items() if k != "call_outcome"}

    update = {"fields_captured": fields_captured}
    if call_outcome:
        update["call_outcome"] = call_outcome
        update["ended_at"] = timezone.now()
    await CallLog.objects.filter(call_sid=call_sid).aupdate(**update)


async def _run_call_twilio_bg(websocket, stream_sid, call_sid, extension="", claim_context=None):
    state["call_sid"] = call_sid

    def on_runner_ready(runner):
        state["runner"] = runner
        state["status"] = "in_progress"

    try:
        await run_call_twilio(
            websocket,
            stream_sid,
            call_sid,
            on_runner_ready=on_runner_ready,
            known_extension=extension,
            claim_context=claim_context,
        )
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
        state["error"] = str(exc)
        state["status"] = "error"
        save_call_outcome_if_unset("error")
    else:
        state["status"] = "ended"
    finally:
        state["runner"] = None
        state["call_sid"] = None
        # NOT clearing call_claim_context[call_sid] here -- see state.py.
        await _sync_call_log(call_sid)


async def websocket_application(scope, receive, send):
    websocket = WebSocket(scope, receive=receive, send=send)
    await websocket.accept()
    try:
        # Twilio sends two handshake messages before real audio starts:
        # "connected" (ignore), then "start" (carries streamSid/callSid).
        messages = websocket.iter_text()
        await messages.__anext__()
        start_data = json.loads(await messages.__anext__())
        stream_sid = start_data["start"]["streamSid"]
        call_sid = start_data["start"]["callSid"]
    except (WebSocketDisconnect, StopAsyncIteration, KeyError):
        return

    # extension is pop-once (only dial it on the first leg); claim_context
    # is a plain get, so it's still there after a DTMF-redirect reconnect.
    extension = call_pending_extension.pop(call_sid, "")
    claim_context = call_claim_context.get(call_sid)
    await _run_call_twilio_bg(
        websocket,
        stream_sid,
        call_sid,
        extension=extension,
        claim_context=claim_context,
    )
