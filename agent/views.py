"""Django views -- a straight port of the FastAPI endpoints in app.py.

main.py (the pipeline/business logic) is framework-agnostic and is reused
unchanged. Only the web-framework glue changes here.
"""

import asyncio
import json
import os
import tempfile

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from main import (
    DEFAULT_CLAIM_CONTEXT,
    RESULTS_FILE,
    clear_results,
    hang_up_twilio_call,
    load_claims_from_excel,
    run_call,
    save_call_outcome_if_unset,
)

from google_auth_oauthlib.flow import Flow

from .decorators import role_required
from .models import CallBatch, CallLog, GoogleDriveAuth, Recording
from .services.batches import (
    batch_running,
    call_in_progress,
    dial_call,
    reconcile_orphaned_batches,
    record_twilio_terminal_status,
    start_batch,
)
from .services.exports import build_call_log_workbook
from .services.recordings import DRIVE_SCOPES, process_recording, stream_recording_bytes
from .state import (
    FIELD_LABELS,
    background_tasks,
    batch_state,
    state,
    uploaded_claims,
)


@login_required
def index(request):
    return render(request, "agent/index.html")


@role_required("admin")
def admin_call_logs(request):
    logs = CallLog.objects.select_related("placed_by").prefetch_related("recordings").all()
    batches = CallBatch.objects.select_related("created_by").annotate(
        total=Count("calls"),
        succeeded=Count("calls", filter=Q(calls__call_outcome="success")),
    )
    drive_connected = GoogleDriveAuth.objects.exists()
    return render(
        request,
        "agent/call_logs.html",
        {"logs": logs, "batches": batches, "drive_connected": drive_connected},
    )


@role_required("admin")
@require_GET
def export_call_logs(request):
    """Download call results as .xlsx -- every call, or with ?batch=<id>
    just that batch in sheet order."""
    logs = CallLog.objects.select_related("placed_by").prefetch_related("recordings")
    batch_id = request.GET.get("batch")
    if batch_id:
        if not batch_id.isdigit() or not CallBatch.objects.filter(id=batch_id).exists():
            raise Http404("No such batch.")
        logs = logs.filter(batch_id=batch_id).order_by("row_number", "id")
        filename = f"call_results_batch_{batch_id}.xlsx"
    else:
        filename = f"call_results_{timezone.now():%Y-%m-%d}.xlsx"

    response = HttpResponse(
        build_call_log_workbook(logs),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _build_oauth_flow(state=None):
    redirect_uri = os.getenv("GOOGLE_DRIVE_REDIRECT_URI")
    client_config = {
        "web": {
            "client_id": os.getenv("GOOGLE_DRIVE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_DRIVE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    return Flow.from_client_config(
        client_config, scopes=DRIVE_SCOPES, redirect_uri=redirect_uri, state=state
    )


@role_required("admin")
def google_drive_authorize(request):
    """Kicks off the one-time Google OAuth consent flow. Needed because
    service accounts can't write to regular Drive storage -- this lets
    uploads act as a real authorized Google user's own account instead."""
    flow = _build_oauth_flow()
    # access_type=offline + prompt=consent guarantees a refresh_token comes
    # back every time (Google otherwise only issues one on a user's very
    # first consent, which isn't reliable to depend on for re-authorizing).
    auth_url, oauth_state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    request.session["google_oauth_state"] = oauth_state
    # authorization_url() generates a PKCE code_verifier and sends Google its
    # hash; the callback builds a fresh Flow, so it has to be handed the same
    # verifier back or the token exchange fails ("Missing code verifier").
    request.session["google_oauth_code_verifier"] = flow.code_verifier
    return HttpResponseRedirect(auth_url)


@role_required("admin")
def google_drive_callback(request):
    """Google redirects here (GOOGLE_DRIVE_REDIRECT_URI) with an
    authorization code after the admin grants consent. Exchanges it for a
    refresh token and stores it in the GoogleDriveAuth singleton row.

    Builds the authorization_response URL from the known, fixed
    GOOGLE_DRIVE_REDIRECT_URI rather than request.build_absolute_uri() --
    behind ngrok, Django's own scheme detection isn't reliable enough to
    trust matching exactly what Google expects back.
    """
    redirect_uri = os.getenv("GOOGLE_DRIVE_REDIRECT_URI")
    authorization_response = f"{redirect_uri}?{request.META.get('QUERY_STRING', '')}"
    flow = _build_oauth_flow(state=request.session.get("google_oauth_state"))
    flow.code_verifier = request.session.get("google_oauth_code_verifier")
    flow.fetch_token(authorization_response=authorization_response)

    refresh_token = flow.credentials.refresh_token
    if not refresh_token:
        return HttpResponse(
            "Google didn't return a refresh token. Try again -- if it still doesn't show up, "
            "revoke this app's access at https://myaccount.google.com/permissions and retry.",
            status=400,
        )

    GoogleDriveAuth.objects.all().delete()
    GoogleDriveAuth.objects.create(refresh_token=refresh_token)
    return HttpResponseRedirect("/call-logs")


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

    if batch_running():
        return JsonResponse({"error": "A batch is running."}, status=409)
    if call_in_progress():
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
        dialed_call_sid=None,  # call_results.json now belongs to this demo, not the last real call
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
    if batch_running():
        return JsonResponse({"error": "A batch is running."}, status=409)
    if call_in_progress():
        return JsonResponse({"error": "A call is already in progress."}, status=409)

    user = await request.auser()  # login_required guarantees this is authenticated
    call_log, error = await dial_call(destination_number, extension, claim_context, user)
    if error:
        return JsonResponse({"error": error}, status=400)
    return JsonResponse({"status": "calling", "call_sid": call_log.call_sid})


# --- Batch dialing ---
# The loop itself is agent/services/batches.py; these just start/stop it and
# report on it.


def _batch_payload(batch, rows):
    finished = [r for r in rows if r.call_outcome]
    return {
        "id": batch.id,
        "status": batch.status,
        "source_filename": batch.source_filename,
        "running": batch.status == CallBatch.STATUS_RUNNING,
        "stop_requested": batch_state["batch_id"] == batch.id and batch_state["stop_requested"],
        "total": len(rows),
        "done": len(finished),
        "succeeded": sum(1 for r in finished if r.call_outcome == "success"),
        "rows": [
            {
                "row_number": r.row_number,
                "destination_number": r.destination_number,
                "extension": r.extension,
                "claim_context": r.claim_context,
                "status": r.status_label,
                "fields_captured": r.fields_captured,
                "error": r.error,
            }
            for r in rows
        ],
    }


@login_required
@csrf_exempt
@require_POST
async def batch_start(request):
    user = await request.auser()
    await reconcile_orphaned_batches()
    if batch_running():
        return JsonResponse({"error": "A batch is already running."}, status=409)
    if call_in_progress():
        return JsonResponse({"error": "A call is already in progress."}, status=409)
    if not uploaded_claims:
        return JsonResponse({"error": "Upload a claims sheet first."}, status=400)
    twilio_env = ("PUBLIC_BASE_URL", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")
    if not all(os.getenv(key) for key in twilio_env):
        # dial_call checks this too, but per row -- catch it once up front
        # rather than marking every row of the sheet as failed.
        return JsonResponse({"error": f"Set {', '.join(twilio_env)} in .env first."}, status=400)

    claims = list(uploaded_claims)
    batch = await start_batch(claims, user, batch_state["upload_filename"])
    return JsonResponse({"status": "started", "batch_id": batch.id, "count": len(claims)})


@login_required
@csrf_exempt
@require_POST
async def batch_stop(request):
    """Stops dialing further rows. The call in progress, if any, is left to
    finish -- End Call hangs that one up."""
    if not batch_running():
        return JsonResponse({"error": "No batch is running."}, status=409)
    batch_state["stop_requested"] = True
    return JsonResponse({"status": "stopping"})


@login_required
@require_GET
async def batch_status(request):
    """The batch to show on the dial page: the one running now, otherwise
    the one started from the sheet that's currently uploaded (so its results
    stay on screen until a new sheet replaces them)."""
    await reconcile_orphaned_batches()
    batch_id = batch_state["batch_id"] or batch_state["upload_batch_id"]
    batch = await CallBatch.objects.filter(id=batch_id).afirst() if batch_id else None
    if batch is None:
        return JsonResponse({"batch": None})
    rows = [r async for r in CallLog.objects.filter(batch=batch).order_by("row_number", "id")]
    return JsonResponse({"batch": _batch_payload(batch, rows)})


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
    await record_twilio_terminal_status(form.get("CallSid", ""), form.get("CallStatus", ""))
    return HttpResponse(status=204)


@csrf_exempt
@require_POST
async def recording_status(request):
    """Twilio's recording-status callback webhook -- not login-gated, same
    reasoning as call_status. Fires once the recording is actually ready to
    download, separately from the call's own completion."""
    form = request.POST
    if form.get("RecordingStatus") != "completed":
        return HttpResponse(status=204)

    call_sid = form.get("CallSid", "")
    recording_sid = form.get("RecordingSid", "")
    duration = form.get("RecordingDuration")
    duration_seconds = int(duration) if duration and duration.isdigit() else None

    if call_sid and recording_sid:
        task = asyncio.create_task(process_recording(call_sid, recording_sid, duration_seconds))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
    return HttpResponse(status=204)


@role_required("admin")
async def stream_recording(request, recording_id):
    recording = await Recording.objects.filter(
        id=recording_id, status=Recording.STATUS_UPLOADED
    ).afirst()
    if recording is None:
        raise Http404("Recording not found or not yet available.")
    content = await stream_recording_bytes(recording.gdrive_file_id)
    response = HttpResponse(content, content_type="audio/mpeg")
    if request.GET.get("download"):
        response["Content-Disposition"] = f'attachment; filename="recording_{recording.twilio_recording_sid}.mp3"'
    return response


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
    if batch_running():
        # The running batch has its own copy of the rows, but the page shows
        # one sheet at a time -- don't swap it out mid-run.
        return JsonResponse(
            {"error": "A batch is running -- wait for it to finish or stop it first."}, status=409
        )

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
    batch_state.update(upload_filename=file.name, upload_batch_id=None)
    return JsonResponse({"status": "loaded", "count": len(claims), "claims": claims})


@login_required
@require_GET
def get_claims(request):
    return JsonResponse({"claims": uploaded_claims})
