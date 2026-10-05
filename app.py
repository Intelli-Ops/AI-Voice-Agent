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
import tempfile

from fastapi import FastAPI, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient

from main import (
    DEFAULT_CLAIM_CONTEXT,
    RESULTS_FILE,
    clear_results,
    hang_up_twilio_call,
    load_claims_from_excel,
    run_call,
    run_call_twilio,
    save_call_outcome_if_unset,
)

app = FastAPI()

state = {
    "status": "idle",  # idle | starting | in_progress | ended | error
    "runner": None,
    "task": None,
    "destination_number": None,
    "extension": "",
    "error": None,
    "call_sid": None,
    "claim_context": None,  # set per-call; falls back to DEFAULT_CLAIM_CONTEXT when idle
}

# call_sid -> {"extension": ..., "claim_context": ...}, consumed (popped) the
# first time /ws sees that call_sid. Needed because dialing a known extension
# now triggers a TwiML redirect that disconnects and reconnects /ws for the
# SAME call -- without popping, the second connection would see the same
# data again and redirect forever.
call_pending_data: dict[str, dict] = {}

# Claims parsed from the last uploaded Excel sheet (see load_claims_from_excel).
# In-memory only, by design -- this is a prototype, not a durable store.
uploaded_claims: list[dict] = []

FIELD_LABELS = {
    "call_outcome": "Call Outcome",
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
    claim_index: int | None = None  # index into uploaded_claims, if placing from the sheet


async def _run_call_bg(claim_context=None):
    def on_runner_ready(runner):
        state["runner"] = runner
        state["status"] = "in_progress"

    try:
        await run_call(on_runner_ready=on_runner_ready, claim_context=claim_context)
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI
        state["error"] = str(exc)
        state["status"] = "error"
        save_call_outcome_if_unset("error")
    else:
        state["status"] = "ended"
    finally:
        state["runner"] = None


@app.post("/api/start-call")
async def start_call(req: StartCallRequest):
    if state["status"] in ("starting", "in_progress"):
        return JSONResponse({"error": "A call is already in progress."}, status_code=409)

    claim_context = DEFAULT_CLAIM_CONTEXT
    if req.claim_index is not None:
        if not (0 <= req.claim_index < len(uploaded_claims)):
            return JSONResponse({"error": f"No uploaded claim at index {req.claim_index}"}, status_code=400)
        claim_context = uploaded_claims[req.claim_index]["claim_context"]

    clear_results()
    state.update(
        status="starting",
        runner=None,
        error=None,
        destination_number=req.destination_number,
        claim_context=claim_context,
    )
    state["task"] = asyncio.create_task(_run_call_bg(claim_context=claim_context))
    return {"status": "starting"}


@app.post("/api/end-call")
async def end_call():
    # Ending our own pipeline (runner.end()) does NOT reliably hang up the
    # real Twilio call -- Pipecat's own auto-hangup only fires if an EndFrame/
    # CancelFrame happens to reach the serializer before shutdown, which isn't
    # guaranteed. Observed in practice: frontend showed "ended" while the
    # receiver's phone was still connected. Hang up explicitly via the REST
    # API so this is guaranteed regardless of pipeline shutdown timing.
    call_sid = state.get("call_sid")
    if call_sid:
        await hang_up_twilio_call(call_sid)
    if state["runner"] is not None:
        await state["runner"].end()
    return {"status": "ending"}


# --- Real telephony (Twilio) ---

async def _run_call_twilio_bg(
    websocket: WebSocket, stream_sid: str, call_sid: str, extension: str = "", claim_context=None
):
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


@app.post("/api/place-call")
async def place_call(req: StartCallRequest):
    destination_number = req.destination_number
    claim_context = DEFAULT_CLAIM_CONTEXT
    extension = req.extension

    # Placing from an uploaded claim overrides the manually-typed fields --
    # the sheet is the source of truth for that row.
    if req.claim_index is not None:
        if not (0 <= req.claim_index < len(uploaded_claims)):
            return JSONResponse({"error": f"No uploaded claim at index {req.claim_index}"}, status_code=400)
        claim_row = uploaded_claims[req.claim_index]
        destination_number = claim_row["destination_number"]
        extension = claim_row["extension"]
        claim_context = claim_row["claim_context"]

    if not destination_number:
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
        destination_number=destination_number,
        extension=extension,
        claim_context=claim_context,
    )

    client = TwilioClient(account_sid, auth_token)
    try:
        call = client.calls.create(
            to=destination_number,
            from_=from_number,
            url=f"{base_url}/twiml",
            # "completed" fires once with the call's FINAL CallStatus --
            # covers busy/failed/no-answer/completed/canceled in one webhook,
            # letting us record an outcome even for calls that never answer
            # and so never reach our websocket at all.
            status_callback=f"{base_url}/call-status",
            status_callback_event=["completed"],
        )
    except Exception as exc:  # noqa: BLE001 -- surface Twilio API errors to the UI
        state.update(status="error", error=str(exc))
        return JSONResponse({"error": str(exc)}, status_code=400)
    call_pending_data[call.sid] = {"extension": extension, "claim_context": claim_context}
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

    pending = call_pending_data.pop(call_sid, {})
    await _run_call_twilio_bg(
        websocket,
        stream_sid,
        call_sid,
        extension=pending.get("extension", ""),
        claim_context=pending.get("claim_context"),
    )


@app.post("/call-status")
async def call_status(request: Request):
    """Twilio's status callback webhook -- fires even for calls that are
    never answered (no-answer/busy/failed), which our /ws handler never
    sees at all since no Media Stream ever connects for those. Without this,
    a call nobody picks up leaves no record of what happened."""
    form = await request.form()
    twilio_status = form.get("CallStatus", "")
    outcome_map = {
        "no-answer": "no_answer",
        "busy": "busy",
        "failed": "failed",
        "canceled": "canceled",
    }
    outcome = outcome_map.get(twilio_status)
    if outcome:
        save_call_outcome_if_unset(outcome)
    return Response(status_code=204)


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
        "claim_context": state["claim_context"] or DEFAULT_CLAIM_CONTEXT,
        "fields": fields,
        "all_field_keys": list(FIELD_LABELS.keys()),
    }


@app.post("/api/upload-claims")
async def upload_claims(file: UploadFile):
    if not file.filename.lower().endswith(".xlsx"):
        return JSONResponse({"error": "Please upload a .xlsx file."}, status_code=400)

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        claims = load_claims_from_excel(tmp_path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        os.unlink(tmp_path)

    if not claims:
        return JSONResponse(
            {"error": "No usable rows found -- check that every row has destination_number, "
                      "client_name, claim_number, doi, and patient_name filled in."},
            status_code=400,
        )

    uploaded_claims.clear()
    uploaded_claims.extend(claims)
    return {"status": "loaded", "count": len(claims), "claims": claims}


@app.get("/api/claims")
async def get_claims():
    return {"claims": uploaded_claims}


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
    <h2 style="font-size:15px;margin:0 0 10px;">Load Claims from Excel</h2>
    <div class="row">
      <input type="file" id="claimsFile" accept=".xlsx">
      <button id="uploadBtn" onclick="uploadClaims()">Upload</button>
      <select id="claimSelect" style="flex:1; min-width:260px; padding:10px 12px; border:1px solid var(--border); border-radius:8px; font-size:14px;" onchange="onClaimSelected()">
        <option value="">— manual entry —</option>
      </select>
    </div>
    <div class="hint">Columns required: destination_number, extension (optional), client_name, claim_number, doi, patient_name.</div>
  </div>

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

let loadedClaims = [];

function selectedClaimIndex() {
  const v = document.getElementById('claimSelect').value;
  return v === '' ? null : parseInt(v, 10);
}

async function uploadClaims() {
  const fileInput = document.getElementById('claimsFile');
  if (!fileInput.files.length) { alert('Choose a .xlsx file first.'); return; }
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  const res = await fetch('/api/upload-claims', {method: 'POST', body: formData});
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  loadedClaims = data.claims;
  populateClaimSelect();
  alert(`Loaded ${data.count} claim(s).`);
}

function populateClaimSelect() {
  const select = document.getElementById('claimSelect');
  select.innerHTML = '<option value="">— manual entry —</option>';
  loadedClaims.forEach((c, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    const cc = c.claim_context;
    opt.textContent = `Row ${c.row_number}: ${cc.client_name} / ${cc.claim_number} / ${cc.patient_name} -> ${c.destination_number}`;
    select.appendChild(opt);
  });
}

function onClaimSelected() {
  const idx = selectedClaimIndex();
  if (idx === null) return;
  const c = loadedClaims[idx];
  document.getElementById('numberInput').value = c.destination_number;
  document.getElementById('extensionInput').value = c.extension;
}

async function startCall() {
  const number = document.getElementById('numberInput').value;
  const claimIndex = selectedClaimIndex();
  lastFilled = new Set();
  await fetch('/api/start-call', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({destination_number: number, claim_index: claimIndex})
  });
}

async function placeCall() {
  const number = document.getElementById('numberInput').value;
  const extension = document.getElementById('extensionInput').value;
  const claimIndex = selectedClaimIndex();
  if (!number && claimIndex === null) { alert('Enter a destination number, or select an uploaded claim.'); return; }
  lastFilled = new Set();
  const res = await fetch('/api/place-call', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({destination_number: number, extension: extension, claim_index: claimIndex})
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

async function loadExistingClaims() {
  const res = await fetch('/api/claims');
  const data = await res.json();
  if (data.claims && data.claims.length) {
    loadedClaims = data.claims;
    populateClaimSelect();
  }
}

setInterval(poll, 1000);
poll();
loadExistingClaims();
</script>
</body>
</html>
"""
