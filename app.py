"""
Web front-end for the voice agent demo.

Serves a page with a "Start Call" button that runs the same local
mic/speaker pipeline as main.py, plus a live-updating panel showing
captured fields as the agent saves them via tool calls.

Run: python app.py
Then open http://localhost:8000
"""

import asyncio
import json
import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient

from main import CLAIM_CONTEXT, RESULTS_FILE, clear_results, run_call, run_call_twilio

app = FastAPI()

state = {
    "status": "idle",  # idle | starting | in_progress | ended | error
    "runner": None,
    "task": None,
    "destination_number": None,
    "extension": "",
    "error": None,
}

FIELD_LABELS = {
    "claim_confirmed": "Claim Confirmed",
    "claim_status": "Claim Status",
    "adjuster_name": "Adjuster Name",
    "adjuster_phone": "Adjuster Phone",
    "adjuster_email": "Adjuster Email",
    "adjuster_fax": "Adjuster Fax",
}


class StartCallRequest(BaseModel):
    destination_number: str = ""
    extension: str = ""


async def _run_call_bg():
    def on_runner_ready(runner):
        state["runner"] = runner
        state["status"] = "in_progress"

    try:
        await run_call(on_runner_ready=on_runner_ready)
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
        state["error"] = str(exc)
        state["status"] = "error"
    else:
        state["status"] = "ended"
    finally:
        state["runner"] = None


@app.post("/api/start-call")
async def start_call(req: StartCallRequest):
    if state["status"] in ("starting", "in_progress"):
        return JSONResponse({"error": "A call is already in progress."}, status_code=409)

    clear_results()
    state.update(status="starting", runner=None, error=None, destination_number=req.destination_number)
    state["task"] = asyncio.create_task(_run_call_bg())
    return {"status": "starting"}


@app.post("/api/end-call")
async def end_call():
    if state["runner"] is not None:
        await state["runner"].end()
    return {"status": "ending"}


# --- Real telephony (Twilio) ---

async def _run_call_twilio_bg(websocket: WebSocket, stream_sid: str, call_sid: str, extension: str = ""):
    def on_runner_ready(runner):
        state["runner"] = runner
        state["status"] = "in_progress"

    try:
        await run_call_twilio(
            websocket, stream_sid, call_sid, on_runner_ready=on_runner_ready, known_extension=extension
        )
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
        state["error"] = str(exc)
        state["status"] = "error"
    else:
        state["status"] = "ended"
    finally:
        state["runner"] = None


@app.post("/api/place-call")
async def place_call(req: StartCallRequest):
    if not req.destination_number:
        return JSONResponse({"error": "destination_number is required"}, status_code=400)
    if state["status"] in ("starting", "in_progress"):
        return JSONResponse({"error": "A call is already in progress."}, status_code=409)

    base_url = os.getenv("PUBLIC_BASE_URL")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_NUMBER")
    if not all([base_url, account_sid, auth_token, from_number]):
        return JSONResponse(
            {"error": "Set PUBLIC_BASE_URL, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
                      "TWILIO_PHONE_NUMBER in .env first."},
            status_code=400,
        )

    clear_results()
    state.update(
        status="starting",
        runner=None,
        error=None,
        destination_number=req.destination_number,
        extension=req.extension,
    )

    client = TwilioClient(account_sid, auth_token)
    try:
        call = client.calls.create(
            to=req.destination_number,
            from_=from_number,
            url=f"{base_url}/twiml",
        )
    except Exception as exc:  # noqa: BLE001 -- surface Twilio API errors to the UI
        state.update(status="error", error=str(exc))
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"status": "calling", "call_sid": call.sid}


@app.post("/twiml")
async def twiml():
    base_url = os.getenv("PUBLIC_BASE_URL", "")
    wss_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f'<Stream url="{wss_url}" />'
        "</Connect></Response>"
    )
    return Response(content=xml, media_type="application/xml")


@app.websocket("/ws")
async def twilio_media_stream(websocket: WebSocket):
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

    await _run_call_twilio_bg(websocket, stream_sid, call_sid, extension=state["extension"])


@app.get("/api/results")
async def get_results():
    fields = {}
    if RESULTS_FILE.exists():
        raw = json.loads(RESULTS_FILE.read_text())
        for key, entry in raw.items():
            fields[key] = {
                "label": FIELD_LABELS.get(key, key),
                "value": entry["value"],
                "captured_at": entry["captured_at"],
            }
    return {
        "status": state["status"],
        "destination_number": state["destination_number"],
        "error": state["error"],
        "claim_context": CLAIM_CONTEXT,
        "fields": fields,
        "all_field_keys": list(FIELD_LABELS.keys()),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


HTML_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Voice Agent Demo</title>
<style>
  :root {
    --bg: #f4f6fb; --card: #ffffff; --border: #e2e6ef; --text: #1a1f2e;
    --muted: #6b7280; --accent: #2f6fed; --accent-dark: #1f4fc4;
    --green: #16a34a; --red: #dc2626; --amber: #d97706;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    background: var(--bg); color: var(--text); padding: 32px;
  }
  .wrap { max-width: 880px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 24px; font-size: 14px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px 24px; margin-bottom: 20px; box-shadow: 0 1px 2px rgba(0,0,0,0.03);
  }
  .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  input[type=text] {
    flex: 1; min-width: 220px; padding: 10px 12px; border: 1px solid var(--border);
    border-radius: 8px; font-size: 14px;
  }
  button {
    padding: 10px 18px; border-radius: 8px; border: none; font-size: 14px;
    font-weight: 600; cursor: pointer; transition: background 0.15s;
  }
  #startBtn { background: var(--accent); color: white; }
  #startBtn:hover { background: var(--accent-dark); }
  #startBtn:disabled { background: #b7c6f2; cursor: not-allowed; }
  #callBtn { background: var(--green); color: white; }
  #callBtn:hover { background: #128a43; }
  #callBtn:disabled { background: #b7e2c6; cursor: not-allowed; }
  #endBtn { background: #fee2e2; color: var(--red); }
  #endBtn:hover { background: #fecaca; }
  #endBtn:disabled { background: #f3f4f6; color: #b8bcc4; cursor: not-allowed; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px;
    border-radius: 999px; font-size: 12px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; }
  .badge-idle { background: #f1f2f6; color: var(--muted); }
  .badge-idle .dot { background: #9ca3af; }
  .badge-starting { background: #fef3c7; color: var(--amber); }
  .badge-starting .dot { background: var(--amber); animation: pulse 1s infinite; }
  .badge-in_progress { background: #dcfce7; color: var(--green); }
  .badge-in_progress .dot { background: var(--green); animation: pulse 1s infinite; }
  .badge-ended { background: #e0e7ff; color: #4338ca; }
  .badge-ended .dot { background: #4338ca; }
  .badge-error { background: #fee2e2; color: var(--red); }
  .badge-error .dot { background: var(--red); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

  .claim-meta { display: flex; gap: 24px; flex-wrap: wrap; font-size: 13px; color: var(--muted); margin-top: 10px; }
  .claim-meta b { color: var(--text); }

  table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  th, td { text-align: left; padding: 10px 8px; font-size: 14px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; }
  td.value { font-weight: 500; }
  td.pending { color: #b8bcc4; font-style: italic; }
  td.captured-at { color: var(--muted); font-size: 12px; }
  tr.just-filled td.value { color: var(--green); }

  .hint { font-size: 13px; color: var(--muted); margin-top: 10px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>AI Voice Agent &mdash; Lien Verification Demo</h1>
  <p class="sub">Local prototype: talks through your mic/speakers, following the same pipeline that will run over real telephony once the calling account is set up.</p>

  <div class="card">
    <div class="row">
      <input type="text" id="numberInput" placeholder="Insurance company number (e.g. +1 555 201 3344)">
      <input type="text" id="extensionInput" placeholder="Extension (if known)" style="max-width:160px;">
      <button id="startBtn" onclick="startCall()">Start Call (mic demo)</button>
      <button id="callBtn" onclick="placeCall()">Place Real Call</button>
      <button id="endBtn" onclick="endCall()" disabled>End Call</button>
      <span class="badge badge-idle" id="statusBadge"><span class="dot"></span><span id="statusText">Idle</span></span>
    </div>
    <div class="claim-meta">
      <span>Client: <b id="metaClient">&mdash;</b></span>
      <span>Claim #: <b id="metaClaim">&mdash;</b></span>
      <span>DOI: <b id="metaDoi">&mdash;</b></span>
      <span>Patient: <b id="metaPatient">&mdash;</b></span>
    </div>
    <div class="hint">Wear headphones. When the agent speaks, play the role of the insurance rep/adjuster.</div>
  </div>

  <div class="card">
    <h2 style="font-size:15px;margin:0 0 4px;">Captured Information</h2>
    <p class="sub" style="margin:0 0 8px;">Updates live as the agent confirms each field on the call.</p>
    <table>
      <thead><tr><th>Field</th><th>Value</th><th>Captured At</th></tr></thead>
      <tbody id="resultsBody"></tbody>
    </table>
  </div>
</div>

<script>
let lastFilled = new Set();

function setStatus(status) {
  const badge = document.getElementById('statusBadge');
  const text = document.getElementById('statusText');
  const labels = {idle: 'Idle', starting: 'Connecting...', in_progress: 'Call In Progress', ended: 'Call Ended', error: 'Error'};
  badge.className = 'badge badge-' + status;
  text.textContent = labels[status] || status;
  const busy = (status === 'starting' || status === 'in_progress');
  document.getElementById('startBtn').disabled = busy;
  document.getElementById('callBtn').disabled = busy;
  document.getElementById('endBtn').disabled = (status !== 'in_progress');
}

async function startCall() {
  const number = document.getElementById('numberInput').value;
  lastFilled = new Set();
  await fetch('/api/start-call', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({destination_number: number})
  });
}

async function placeCall() {
  const number = document.getElementById('numberInput').value;
  const extension = document.getElementById('extensionInput').value;
  if (!number) { alert('Enter a destination number first.'); return; }
  lastFilled = new Set();
  const res = await fetch('/api/place-call', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({destination_number: number, extension: extension})
  });
  const data = await res.json();
  if (data.error) alert(data.error);
}

async function endCall() {
  await fetch('/api/end-call', {method: 'POST'});
}

async function poll() {
  const res = await fetch('/api/results');
  const data = await res.json();
  setStatus(data.status);

  const ctx = data.claim_context || {};
  document.getElementById('metaClient').textContent = ctx.client_name || '—';
  document.getElementById('metaClaim').textContent = ctx.claim_number || '—';
  document.getElementById('metaDoi').textContent = ctx.doi || '—';
  document.getElementById('metaPatient').textContent = ctx.patient_name || '—';

  const body = document.getElementById('resultsBody');
  body.innerHTML = '';
  for (const key of data.all_field_keys) {
    const f = data.fields[key];
    const tr = document.createElement('tr');
    if (f && !lastFilled.has(key)) { tr.className = 'just-filled'; lastFilled.add(key); }
    const label = (f && f.label) || key;
    if (f) {
      tr.innerHTML = `<td>${label}</td><td class="value">${f.value}</td><td class="captured-at">${new Date(f.captured_at).toLocaleTimeString()}</td>`;
    } else {
      tr.innerHTML = `<td>${label}</td><td class="pending">waiting...</td><td></td>`;
    }
    body.appendChild(tr);
  }
}

setInterval(poll, 1000);
poll();
</script>
</body>
</html>
"""
