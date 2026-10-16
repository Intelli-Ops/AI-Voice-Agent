"""Twilio Media Stream WebSocket handler -- a straight port of app.py's
/ws FastAPI endpoint, wrapping the raw ASGI scope/receive/send in a
starlette.websockets.WebSocket so pipecat's FastAPIWebsocketTransport (which
only needs that interface) works completely unchanged.
"""

import asyncio
import json

from starlette.websockets import WebSocket, WebSocketDisconnect

from main import run_call_twilio, save_call_outcome_if_unset

from .services.batches import sync_call_results
from .state import (
    MAX_EXTENSION_LEGS,
    call_claim_context,
    call_extension_legs,
    call_pending_extension,
    call_runners,
    call_terminal_status,
    state,
)


async def _run_call_twilio_bg(websocket, stream_sid, call_sid, extension="", claim_context=None):
    state["call_sid"] = call_sid
    state["ws_active"] += 1
    runners = call_runners.setdefault(call_sid, set())
    runner_ref = {}

    def on_runner_ready(runner):
        runner_ref["runner"] = runner
        runners.add(runner)
        state["runner"] = runner
        state["status"] = "in_progress"

    # The live state and call_results.json belong to whichever call was
    # placed last. A leg that finishes late, after another call has been
    # placed, must leave both alone.
    def is_current_call():
        return state["dialed_call_sid"] in (None, call_sid)

    try:
        await run_call_twilio(
            websocket,
            stream_sid,
            call_sid,
            on_runner_ready=on_runner_ready,
            known_extension=extension,
            claim_context=claim_context,
        )
    except asyncio.CancelledError:
        # Cancelled by batches._cancel_lingering_pipelines: the call was
        # already over. Not an error -- fall through to the cleanup below.
        pass
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
        if is_current_call():
            state["error"] = str(exc)
            state["status"] = "error"
            save_call_outcome_if_unset("error")
    else:
        if is_current_call():
            state["status"] = "ended"
    finally:
        runners.discard(runner_ref.get("runner"))
        if not runners:
            call_runners.pop(call_sid, None)
        if state["call_sid"] == call_sid:
            state["runner"] = None
            state["call_sid"] = None
        # NOT clearing call_claim_context[call_sid] here -- see state.py.
        try:
            await sync_call_results(call_sid, call_over=call_terminal_status.get(call_sid) == "completed")
        finally:
            # Only after the sync: the batch dialer treats ws_active == 0 as
            # "this call's results are safely in the database", and clears
            # call_results.json for the next row right after.
            state["ws_active"] -= 1


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

    # The extension goes to the first MAX_EXTENSION_LEGS legs only (see
    # state.py); claim_context is kept for every leg of the call.
    extension = ""
    if call_extension_legs.get(call_sid, 0) < MAX_EXTENSION_LEGS:
        extension = call_pending_extension.get(call_sid, "")
        if extension:
            call_extension_legs[call_sid] = call_extension_legs.get(call_sid, 0) + 1
    claim_context = call_claim_context.get(call_sid)
    await _run_call_twilio_bg(
        websocket,
        stream_sid,
        call_sid,
        extension=extension,
        claim_context=claim_context,
    )
