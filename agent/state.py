"""In-memory, process-local state for the voice agent.

Carried over as-is from the FastAPI prototype (app.py) -- these are
deliberately plain module-level dicts, not Django models, since this is
still a single-process prototype, not a durable store.
"""

state = {
    "status": "idle",  # idle | starting | in_progress | ended | error
    "runner": None,
    "task": None,
    "destination_number": None,
    "extension": "",
    "error": None,
    "call_sid": None,
    "claim_context": None,  # set per-call; falls back to DEFAULT_CLAIM_CONTEXT when idle
    # The Twilio call most recently dialed from here. Unlike call_sid above
    # (only set while a /ws leg is connected) this is known from the moment
    # the call is placed, so Twilio's status webhook can tell whether a
    # callback is about the current call or a late one for an earlier call.
    # It is also the owner of call_results.json: that file is cleared when a
    # call is placed, so it only holds this call's results.
    "dialed_call_sid": None,
    # Number of /ws handlers currently running. A counter, not a flag: on a
    # DTMF-redirect reconnect the new leg can connect before the old one has
    # finished tearing down.
    "ws_active": 0,
}

# call_sid -> the known extension to dial for that call. Dialing it triggers
# a TwiML redirect that disconnects and reconnects /ws for the SAME call, so
# each new leg could see it again. call_extension_legs counts the legs that
# have been handed the extension: the first leg dials it; the second one
# only redials if the phone menu is detected again (e.g. "We did not receive
# an extension"), and no leg after that gets it -- so it can't loop forever.
call_pending_extension: dict[str, str] = {}
call_extension_legs: dict[str, int] = {}
MAX_EXTENSION_LEGS = 2

# call_sid -> claim_context, kept (NOT popped) for the lifetime of the call.
# Must survive every /ws reconnect (not just the first leg) -- see app.py's
# original comment history for why this is a separate dict from the one above.
call_claim_context: dict[str, dict] = {}

# call_sid -> the pipeline runner(s) currently serving that call's /ws legs.
# Normally at most one, but a leg doesn't always shut down when Twilio drops
# its websocket -- see batches._cancel_lingering_pipelines.
call_runners: dict[str, set] = {}

# call_sid -> Twilio's final CallStatus (completed/no-answer/busy/failed/
# canceled), recorded by the /call-status webhook. This is the only signal
# that a call is over for good -- /ws disconnecting is not, since that also
# happens mid-call on a DTMF redirect. The batch dialer waits on this before
# moving to the next row.
call_terminal_status: dict[str, str] = {}

# Claims parsed from the last uploaded Excel sheet (see load_claims_from_excel).
# In-memory only, by design -- this is a prototype, not a durable store.
uploaded_claims: list[dict] = []

# The batch dialer (agent/services/batches.py). One batch at a time, run as
# an asyncio task in this process -- the CallBatch/CallLog rows are the
# durable record, this is just the handle on the loop that's driving it.
batch_state = {
    "batch_id": None,  # the CallBatch currently being dialed, if any
    "task": None,
    "starting": False,  # True while a new batch's rows are being created
    "stop_requested": False,
    "upload_filename": "",  # name of the sheet uploaded_claims came from
    "upload_batch_id": None,  # the batch started from the current upload, if any
}

# Keeps a strong reference to fire-and-forget asyncio.create_task() calls
# (e.g. the recording-upload pipeline kicked off from a webhook) so they
# aren't garbage-collected mid-flight -- a bare create_task() result with
# nothing holding onto it is only weakly referenced by the event loop.
background_tasks: set = set()

FIELD_LABELS = {
    "call_outcome": "Call Outcome",
    "claim_confirmed": "Claim Confirmed",
    "claim_status": "Claim Status",
    "adjuster_name": "Adjuster Name",
    "adjuster_phone": "Adjuster Phone",
    "adjuster_email": "Adjuster Email",
    "adjuster_fax": "Adjuster Fax",
    "rep_name": "Rep Name",
    "reference_number": "Reference #",
}
