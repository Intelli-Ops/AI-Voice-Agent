"""Twilio Media Stream WebSocket handler -- a straight port of app.py's
/ws FastAPI endpoint, wrapping the raw ASGI scope/receive/send in a
starlette.websockets.WebSocket so pipecat's FastAPIWebsocketTransport (which
only needs that interface) works completely unchanged.
"""

import json

from starlette.websockets import WebSocket, WebSocketDisconnect

from main import run_call_twilio, save_call_outcome_if_unset

from .state import call_claim_context, call_pending_extension, state


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
