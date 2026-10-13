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
}

# call_sid -> extension, consumed (POPPED) the first time /ws sees that
# call_sid. Needed because dialing a known extension triggers a TwiML
# redirect that disconnects and reconnects /ws for the SAME call -- without
# popping, the second connection would see the same extension again and
# redirect forever.
call_pending_extension: dict[str, str] = {}

# call_sid -> claim_context, kept (NOT popped) for the lifetime of the call.
# Must survive every /ws reconnect (not just the first leg) -- see app.py's
# original comment history for why this is a separate dict from the one above.
call_claim_context: dict[str, dict] = {}

# Claims parsed from the last uploaded Excel sheet (see load_claims_from_excel).
# In-memory only, by design -- this is a prototype, not a durable store.
uploaded_claims: list[dict] = []

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
}
