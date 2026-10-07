"""Django views -- a straight port of the FastAPI endpoints in app.py.

main.py (the pipeline/business logic) is framework-agnostic and is reused
unchanged. Only the web-framework glue changes here.
"""

import asyncio
import json
import os
import tempfile

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from twilio.rest import Client as TwilioClient

from main import (
    DEFAULT_CLAIM_CONTEXT,
    RESULTS_FILE,
    clear_results,
    hang_up_twilio_call,
    load_claims_from_excel,
    run_call,
    save_call_outcome_if_unset,
)

from .decorators import role_required
from .models import CallLog
from .state import FIELD_LABELS, call_claim_context, call_pending_extension, state, uploaded_claims


@login_required
def index(request):
    return render(request, "agent/index.html")


@role_required("admin")
def admin_call_logs(request):
    logs = CallLog.objects.select_related("placed_by").all()
    return render(request, "agent/call_logs.html", {"logs": logs})


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


@login_required
@csrf_exempt
@require_POST
async def start_call(request):
    body = json.loads(request.body or b"{}")
    destination_number = body.get("destination_number", "")
    claim_index = body.get("claim_index")

    if state["status"] in ("starting", "in_progress"):
        return JsonResponse({"error": "A call is already in progress."}, status=409)

    claim_context = DEFAULT_CLAIM_CONTEXT
    if claim_index is not None:
        if not (0 <= claim_index < len(uploaded_claims)):
            return JsonResponse({"error": f"No uploaded claim at index {claim_index}"}, status=400)
        claim_context = uploaded_claims[claim_index]["claim_context"]

    clear_results()
    state.update(
        status="starting",
        runner=None,
        error=None,
        destination_number=destination_number,
        claim_context=claim_context,
    )
    state["task"] = asyncio.create_task(_run_call_bg(claim_context=claim_context))
    return JsonResponse({"status": "starting"})


@login_required
@csrf_exempt
@require_POST
async def end_call(request):
    # Ending our own pipeline (runner.end()) does NOT reliably hang up the
    # real Twilio call -- hang up explicitly via the REST API so this is
    # guaranteed regardless of pipeline shutdown timing.
    call_sid = state.get("call_sid")
    if call_sid:
        await hang_up_twilio_call(call_sid)
    if state["runner"] is not None:
        await state["runner"].end()
    return JsonResponse({"status": "ending"})


# --- Real telephony (Twilio) ---
# The /ws Media Stream handler itself lives in agent/ws_server.py (mounted
# directly in voiceagent/asgi.py) -- it isn't a regular Django view since it
# needs a raw ASGI websocket scope, not Channels.


@login_required
@csrf_exempt
@require_POST
async def place_call(request):
    body = json.loads(request.body or b"{}")
    destination_number = body.get("destination_number", "")
    extension = body.get("extension", "")
    claim_index = body.get("claim_index")
    claim_context = DEFAULT_CLAIM_CONTEXT

    # Placing from an uploaded claim overrides the manually-typed fields --
    # the sheet is the source of truth for that row.
    if claim_index is not None:
        if not (0 <= claim_index < len(uploaded_claims)):
            return JsonResponse({"error": f"No uploaded claim at index {claim_index}"}, status=400)
        claim_row = uploaded_claims[claim_index]
        destination_number = claim_row["destination_number"]
        extension = claim_row["extension"]
        claim_context = claim_row["claim_context"]

    if not destination_number:
        return JsonResponse({"error": "destination_number is required"}, status=400)
    if state["status"] in ("starting", "in_progress"):
        return JsonResponse({"error": "A call is already in progress."}, status=409)

    base_url = os.getenv("PUBLIC_BASE_URL")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_NUMBER")
    if not all([base_url, account_sid, auth_token, from_number]):
        return JsonResponse(
            {
                "error": "Set PUBLIC_BASE_URL, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
                "TWILIO_PHONE_NUMBER in .env first."
            },
            status=400,
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
            status_callback=f"{base_url}/call-status",
            status_callback_event=["completed"],
        )
    except Exception as exc:  # noqa: BLE001 -- surface Twilio API errors to the UI
        state.update(status="error", error=str(exc))
        return JsonResponse({"error": str(exc)}, status=400)

    call_pending_extension[call.sid] = extension
    call_claim_context[call.sid] = claim_context

    user = await request.auser()  # login_required guarantees this is authenticated
    await CallLog.objects.acreate(
        call_sid=call.sid,
        destination_number=destination_number,
        extension=extension,
        claim_context=claim_context,
        placed_by=user,
    )

    return JsonResponse({"status": "calling", "call_sid": call.sid})


@csrf_exempt
@require_POST
def twiml(request):
    base_url = os.getenv("PUBLIC_BASE_URL", "")
    wss_url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f'<Stream url="{wss_url}" />'
        "</Connect></Response>"
    )
    return HttpResponse(content=xml, content_type="application/xml")


@csrf_exempt
@require_POST
async def call_status(request):
    """Twilio's status callback webhook -- fires even for calls that are
    never answered (no-answer/busy/failed), which /ws never sees at all
    since no Media Stream ever connects for those. Not login-gated --
    Twilio calls this directly and can't supply a session cookie."""
    form = request.POST
    call_sid = form.get("CallSid", "")
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
        if call_sid:
            await CallLog.objects.filter(call_sid=call_sid, call_outcome="").aupdate(
                call_outcome=outcome, ended_at=timezone.now()
            )
    return HttpResponse(status=204)


@login_required
@require_GET
def get_results(request):
    fields = {}
    if RESULTS_FILE.exists():
        raw = json.loads(RESULTS_FILE.read_text())
        for key, entry in raw.items():
            fields[key] = {
                "label": FIELD_LABELS.get(key, key),
                "value": entry["value"],
                "captured_at": entry["captured_at"],
            }
    return JsonResponse(
        {
            "status": state["status"],
            "destination_number": state["destination_number"],
            "error": state["error"],
            "claim_context": state["claim_context"] or DEFAULT_CLAIM_CONTEXT,
            "fields": fields,
            "all_field_keys": list(FIELD_LABELS.keys()),
        }
    )


@login_required
@csrf_exempt
@require_POST
def upload_claims(request):
    file = request.FILES.get("file")
    if not file or not file.name.lower().endswith(".xlsx"):
        return JsonResponse({"error": "Please upload a .xlsx file."}, status=400)

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        for chunk in file.chunks():
            tmp.write(chunk)
        tmp_path = tmp.name

    try:
        claims = load_claims_from_excel(tmp_path)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    finally:
        os.unlink(tmp_path)

    if not claims:
        return JsonResponse(
            {
                "error": "No usable rows found -- check that every row has destination_number, "
                "client_name, claim_number, doi, and patient_name filled in."
            },
            status=400,
        )

    uploaded_claims.clear()
    uploaded_claims.extend(claims)
    return JsonResponse({"status": "loaded", "count": len(claims), "claims": claims})


@login_required
@require_GET
def get_claims(request):
    return JsonResponse({"claims": uploaded_claims})
