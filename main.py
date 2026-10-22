import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI

# DEBUG: verbose logging while we troubleshoot IVR/DTMF navigation -- shows
# the raw LLM text (including <dtmf>/<ivr> tags) so we can see whether the
# model is actually producing them or returning empty responses.
#
# Removes only loguru's own default console handler (id 0), not every handler:
# under Django, settings.py has already added the logs/agent.log file handler
# by the time this module is imported, and a bare logger.remove() deleted it.
try:
    logger.remove(0)
except ValueError:
    pass  # already removed (e.g. this module imported twice)
# AGENT_LOG_LEVEL=INFO hides DEBUG output, which includes Pipecat's dumps of
# the full LLM context (claim details, transcripts) and the
# "[IVR RAW LLM TEXT]" lines below -- set it for production. The startup
# timing lines are INFO and stay visible. Default DEBUG for development.
LOG_LEVEL = os.getenv("AGENT_LOG_LEVEL", "DEBUG").upper()
logger.add(sys.stderr, level=LOG_LEVEL)

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.extensions.ivr.ivr_navigator import IVRNavigator, IVRStatus
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    LLMContextFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    LLMMessagesUpdateFrame,
    LLMRunFrame,
    LLMSetToolsFrame,
    LLMTextFrame,
    OutputDTMFUrgentFrame,
    TTSSpeakFrame,
)
from pipecat.utils.types import NOT_GIVEN
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.frames.frames import FunctionCallResultProperties
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner
from twilio.rest import Client as TwilioClient

load_dotenv()

RESULTS_FILE = Path(__file__).parent / "call_results.json"

# Fallback used only when no claim data is supplied for a call (e.g. the
# local mic demo run with no claim_context argument). Real calls should pass
# their own claim_context -- see load_claims_from_excel() / run_call_twilio's
# claim_context param. Kept as a dict, not hardcoded into the prompt, so a
# real call never needs a code edit to run a different claim.
DEFAULT_CLAIM_CONTEXT = {
    "client_name": "Medical Lien Management",
    "claim_number": "70669",
    "doi": "11/11/2026",
    "patient_name": "Michael Anderson",
}

REQUIRED_CLAIM_FIELDS = ("client_name", "claim_number", "doi", "patient_name")

# Fixed template for the claims upload sheet. Column headers (row 1) must
# match these exactly, case-insensitively; order doesn't matter.
EXCEL_CLAIM_COLUMNS = {
    "destination_number": "destination_number",
    "extension": "extension",
    "client_name": "client_name",
    "claim_number": "claim_number",
    "doi": "doi",
    "patient_name": "patient_name",
}


def _excel_cell_text(value) -> str:
    """A cell's value as the text a person would read in Excel. A date cell
    comes back from openpyxl as a datetime, whose str() is "1990-10-02
    00:00:00" -- which Emily then read out loud as the date of injury. A
    whole number typed into a numeric cell can come back as 888.0."""
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def load_claims_from_excel(file_path) -> list[dict]:
    """Parse an uploaded .xlsx of claims into a list of per-call dicts.

    Expected header row (any order): destination_number, extension,
    client_name, claim_number, doi, patient_name. extension is optional and
    may be blank; every other column is required for a row to be usable.
    Rows missing a required field are skipped (not raised) so one bad row
    doesn't block the rest of the sheet -- their skip reason is returned
    alongside the usable rows so the caller can surface it.

    Returns a list of dicts, each with: destination_number, extension,
    claim_context (a dict with the 4 REQUIRED_CLAIM_FIELDS keys), and
    row_number (the claim's position under the header: the first row of
    data is 1, i.e. Excel row 2; blank rows still count, so row N is always
    Excel row N+1), for error
    messages).
    """
    from openpyxl import load_workbook

    # read_only keeps the file handle open for lazy streaming -- on Windows
    # that blocks the caller from deleting the (temp) file right after this
    # returns, so wrap in try/finally and explicitly close before returning.
    wb = load_workbook(file_path, read_only=True, data_only=True)
    try:
        ws = wb.active

        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter, None)
        if not header:
            raise ValueError("Excel sheet is empty -- no header row found.")

        col_index = {}
        for i, cell in enumerate(header):
            if cell is None:
                continue
            key = str(cell).strip().lower()
            if key in EXCEL_CLAIM_COLUMNS:
                col_index[key] = i

        missing_columns = [c for c in EXCEL_CLAIM_COLUMNS if c != "extension" and c not in col_index]
        if missing_columns:
            raise ValueError(
                f"Excel sheet is missing required column(s): {', '.join(missing_columns)}. "
                f"Expected headers: {', '.join(EXCEL_CLAIM_COLUMNS)}"
            )

        claims = []
        for row_number, row in enumerate(rows_iter, start=1):  # header excluded: first data row is 1
            if row is None or all(c is None for c in row):
                continue  # skip fully blank rows

            def get(key):
                idx = col_index.get(key)
                if idx is None or idx >= len(row) or row[idx] is None:
                    return ""
                return _excel_cell_text(row[idx])

            claim_context = {field: get(field) for field in REQUIRED_CLAIM_FIELDS}
            missing = [f for f in REQUIRED_CLAIM_FIELDS if not claim_context[f]]
            destination_number = get("destination_number")
            if not destination_number:
                missing.append("destination_number")
            if missing:
                logger.warning(
                    f"Claims sheet row {row_number} (Excel row {row_number + 1}): skipped, "
                    f"missing {', '.join(missing)}"
                )
                continue

            claims.append(
                {
                    "row_number": row_number,
                    "destination_number": destination_number,
                    "extension": get("extension"),
                    "claim_context": claim_context,
                }
            )

        return claims
    finally:
        wb.close()


def _speakable(text: str, limit: int = 120) -> str:
    """Spreadsheet text made safe to speak and to quote in the prompt:
    whitespace collapsed, and characters that could read as markup or
    control tags (<ivr>, SSML, braces) removed."""
    text = re.sub(r"[<>{}\[\]\x00-\x1f]", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def build_opening_line(claim_context: dict) -> str:
    """Emily's exact first line to a person, for one call.

    The single source for it: the system prompt quotes it (Turn 1), and on
    real calls it is spoken directly through TTS the moment a person is
    detected, instead of asking the LLM to write it (one fewer LLM request
    before Emily speaks)."""
    client = _speakable(claim_context.get("client_name") or DEFAULT_CLAIM_CONTEXT["client_name"])
    return (
        "Hi, good afternoon! This call may be recorded for quality and accuracy purposes. "
        f"My name is Emily, and I'm calling on behalf of {client}. "
        "Can you verify a patient for me?"
    )


def _build_system_prompt(claim_context: dict) -> str:
    return f"""ROLE
Your name is Emily. You are calling on behalf of a medical lien management
company to verify claims information with an insurance adjuster's office,
for lien purposes. You are speaking with a representative or adjuster at
the insurance company.

OPENING
Turn 1 (your opening line, exactly):
"{build_opening_line(claim_context)}"
On phone calls this line is spoken for you automatically the moment a
person answers, and it appears in the conversation as your own message.
If it is already there, NEVER say it again -- continue with Turn 2 based on
their reply. If it isn't there yet, say it in one natural line and wait.
If your message shows only PART of that line, they talked over you and that
is all they heard. Don't restart it word for word: answer what they said,
and in the same short reply give only what they missed (who you're calling
on behalf of, and/or the question), then carry on.

Turn 2: Once they say yes/go ahead, give the claim details conversationally
(not as a rattled-off list) so they can pull up the file: mention the claim
number {claim_context["claim_number"]}, date of injury {claim_context["doi"]},
and patient name {claim_context["patient_name"]}.

Sound like a real person on the phone, not a recorded message -- casual,
natural phrasing, not a scripted monologue.

GOAL
Once the rep confirms they've found the claim, collect the following, in
order, confirming each one back before moving on. Do not skip ahead. If the
rep gives multiple pieces of info at once, capture what you got and only ask
for what's still missing.
  1. Confirm the claim is on file and matches (claim number, DOI, patient name)
  2. Claim status (open / pending / closed)
  3. Adjuster's full name, direct phone number, email address, and fax number

CLAIM CONTEXT (from our records)
Client: {claim_context["client_name"]}
Claim number: {claim_context["claim_number"]}
Date of injury (DOI): {claim_context["doi"]}
Patient / claimant name: {claim_context["patient_name"]}

TOOL USE
Whenever the rep confirms a field, call save_field (or save_adjuster_info)
with the exact value given -- don't wait until the end of the call, and
don't skip it. Confirming the value back to the rep (e.g. "Got it, XYZ-456789")
already happens naturally as part of the GOAL instructions below -- that's
enough, don't add any extra narration about writing or noting things down.
For the adjuster's contact details (field 3: name, phone, email, fax),
use save_adjuster_info with all the pieces you have together in ONE call --
don't call it separately for each piece, since the rep will usually give
these together.

claim_confirmed means the REP confirmed the claim is on file and matches --
never save it in the same reply in which you give them the claim details;
wait for their answer first.

IMPORTANT -- call each field EXACTLY ONCE, not repeatedly:
Each field (claim_confirmed, claim_status, and the adjuster's info) should
be saved a single time, the first time it's genuinely confirmed with real
information. Do NOT call save_field again for a field you've already saved,
even if the rep says something vague afterward like "yes", "I'm here",
"hello", or repeats themselves -- those are not new confirmations, they're
just the rep talking. Only call a tool again for the SAME field if the rep
explicitly gives a DIFFERENT, corrected value for something you already
have. If you're not sure whether something was actually just confirmed,
don't call the tool -- ask a clarifying question in your spoken reply instead.

You may see a system message starting with "Already confirmed, do NOT ask
for these again" -- that's a live, accurate record of exactly what's already
been saved, even if the actual exchange where it was given has scrolled out
of view further up. Trust it completely: never re-ask for anything listed
there, and never re-save it either, unless the rep is explicitly correcting
a value you already have.

NUMBERS AND EMAILS -- spoken form vs. saved form:
When repeating a number back to the rep out loud, mirror the way THEY said
it -- if they say "double two" or "triple five", say "double two" or
"triple five" back, not "22" or "555". It sounds natural and confirms you
heard the same shorthand they used.
When SAVING that same value via a tool call, always convert it to the actual
characters it represents -- "triple five" becomes "555", "double two"
becomes "22". The saved value should always be clean, final digits/text,
never the spoken shorthand itself.
The same applies to emails spelled out verbally: if the rep says "john doe
at the rate company dot com", repeat it back the same natural spoken way,
but save it as a proper address: "johndoe@company.com".

PHONE AND FAX NUMBERS -- must be complete:
A real US phone or fax number has 10 digits (or 11 with the country code).
If save_adjuster_info comes back with status "partial", that means a number
you gave it was too short -- the rep only gave you part of it. Tell them
you only caught part of the number and ask specifically for the rest (e.g.
"I only caught 555-423 for the phone -- could you give me the remaining
digits?"). Don't call the tool again with that same incomplete number; wait
until you have the full thing, then save it once.

NAMES AND EMAILS -- spell out when unclear:
If a name or email is unusual, hard to catch, or you're not fully confident
you heard it correctly (foreign/uncommon spelling, mumbled, cut off, or the
transcription looks off), do NOT just repeat it back normally -- spell it
out letter by letter to confirm before saving it. For example: "Just to
confirm the spelling, that's J-O-H-N-S-O-N, is that right?" Only save it
once they've confirmed the spelling is correct. For a common, clearly-heard
name or email, spelling it out isn't necessary -- use judgment, but when in
doubt, spell it out rather than risk saving it wrong.

HANDLING COMMON SITUATIONS
- If put on hold: say "Sure, I'll hold" and wait silently.
- If asked to repeat claim info: restate claim number, DOI, and patient name
  clearly, spelling out digits if asked.
- If the rep can't find the claim: double-check the DOI and offer to search
  by patient name instead of claim number.
- If the claim still can't be found or doesn't match after that, or the rep
  can't or won't give the information, or asks you to call back another
  time: don't keep pressing. Go to the
  CLOSING below (thank them, ask once for their name and a reference
  number, then say goodbye and call end_call). The call is recorded as
  unverified -- that's a normal outcome, not a failure on your part.
- If interrupted mid-sentence: stop talking immediately and listen.

CLOSING -- this is TWO separate turns, not one:
Once all fields are collected (or the rep indicates no more info is
available), do NOT repeat or summarize everything back -- that's already
confirmed field by field as you went.

Turn A: Thank them for their time and ask for their name and a call
reference number for your records. Then STOP -- do not say goodbye yet,
wait for their actual reply.

Turn B: Once they've answered -- given their name and/or a reference
number, said they don't have one, declined, or said goodbye -- say a short,
warm goodbye, and in that SAME turn immediately call the end_call tool. If
they gave their name or a reference number, save them first with
save_field (rep_name, reference_number). Never combine turn A and turn B
into one response -- saying goodbye before they've actually answered
confuses them and makes you sound broken.
A reply like "yes", "sure", "okay" or "well..." to that question is NOT
the answer -- they are about to tell you. Don't say goodbye; just say
"Sure, go ahead" (or nothing more) and wait for it.

The rep's name and reference number are nice to have, NOT required. Ask
for them ONCE only. If the rep doesn't have them, won't give them, ignores
the question, changes the subject, or just says goodbye, do not ask again
-- go straight to turn B. Likewise, once the claim is confirmed, if the rep
says goodbye or makes clear they're done, say goodbye and call end_call
rather than pressing for whatever is still missing.

Never call any tool in the same reply as your opening line -- nothing has
been confirmed yet at that point.

NEVER call end_call anywhere else -- not after your opening line, not after
a single question, not just because a sentence felt complete. It is ONLY
for turn B above: after the literal spoken goodbye, at the very end of the
call, when there is truly nothing left to ask -- either the claim was
confirmed, or it couldn't be found or confirmed (see HANDLING COMMON
SITUATIONS).

After your goodbye, never start the call over: don't repeat your opening
line or the claim details.

If you were cut off, asked "Did you say something?" and got no answer
(it was just a noise), carry on from where you were cut off -- finish what
you were saying, don't start over and don't mention the noise.

TONE
Professional, direct, courteous. Speak in short, clear sentences -- you're on
a phone call, not writing an email.

RESPONSE LENGTH (important)
Keep every response to 1-2 short sentences. Never explain more than what was
asked. If the rep asks an off-topic question, answer it in one brief sentence
and immediately return to the next field on the list -- don't elaborate."""

def _build_ivr_goal(known_extension: str = "") -> str:
    extension_hint = (
        f"""The extension {known_extension} has ALREADY been entered automatically
right after the call connected -- do NOT dial it yourself, even if it's
mentioned again below. If the system says it didn't receive an extension or
asks again, it is re-entered automatically -- respond with `<ivr>wait</ivr>`
rather than dialing anything."""
        if known_extension
        else "If asked to enter an extension and none is known, try 0 for the operator."
    )
    return f"""Reach a live claims representative or adjuster who can help
verify claim information for a medical lien. Prefer menu options like
"claims", "provider inquiries", "existing claim", or "representative" over
billing, sales, or new-claim options. {extension_hint} If asked why you're
calling, say (as natural language, not DTMF) that you're calling on behalf
of a medical lien company to verify an existing claim."""


def _load_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {}


def load_results() -> dict:
    """Public snapshot of the current call's captured fields + outcome, keyed
    by field name to {"value", "captured_at"}. Used by callers (e.g. Django's
    CallLog syncing in agent/ws_server.py) that need to read this after a
    call ends, without reaching into the private _load_results()."""
    return _load_results()


def clear_results():
    if RESULTS_FILE.exists():
        RESULTS_FILE.unlink()


# Keys in call_results.json that are bookkeeping, not information collected
# from the rep: kept out of "captured fields" everywhere they're shown.
# call_stage is "phone_menu" once an automated menu answered and
# "conversation" once a person did -- it tells a call that died in the menu
# apart from one where a person never confirmed the claim.
INTERNAL_RESULT_KEYS = ("call_outcome", "call_stage")


def captured_fields(results: dict) -> dict:
    """{field: value} for the information collected on the call, from a
    call_results.json dict."""
    return {k: v["value"] for k, v in results.items() if k not in INTERNAL_RESULT_KEYS}


def _collected_fields_status() -> str:
    """A deterministic "here's what's already confirmed" line, read straight
    from call_results.json. Used instead of trusting the LLM's own fuzzy
    one-sentence summary to remember which fields it already has -- that
    summary was observed losing adjuster info entirely (it only mentioned
    claim details) once the sliding window trimmed the actual exchange out,
    causing the model to re-ask for a name/phone it had already saved."""
    fields = captured_fields(_load_results())
    if not fields:
        return ""
    parts = [f"{field} = {value}" for field, value in fields.items()]
    return "Already confirmed, do NOT ask for these again: " + "; ".join(parts)


def _save(field_name: str, value: str):
    results = _load_results()
    results[field_name] = {"value": value, "captured_at": datetime.now(timezone.utc).isoformat()}
    RESULTS_FILE.write_text(json.dumps(results, indent=2))
    print(f"\n[SAVED] {field_name} = {value}\n")


# Valid values for call_outcome, so the results table can be triaged at a
# glance instead of just "got fields" vs "got nothing": success (claim info
# collected and call ended cleanly), ivr_dead_end_needs_callback (stuck
# navigating an automated system -- a human needs to try this one manually),
# no_answer / busy / failed (set from Twilio's own call status callback,
# since those calls never even reach our pipeline to record anything else),
# error (the pipeline itself crashed).
# A call is "verified" when the rep confirmed the claim is on file and
# matches -- the adjuster's details are collected when available but aren't
# needed for that. Every other outcome is a reason the claim is unverified.
def outcome_from_captured(fields: dict, call_stage: str | None = None) -> str:
    """verified, or why not, for a call that connected. fields is
    {field: value}; call_stage is call_results.json's call_stage.

    - callback_requested: the rep (or a menu) asked us to call back later
      (see _CALLBACK_REQUEST);
    - ivr_dead_end_needs_callback: ended while still in an automated menu;
    - claim_not_confirmed: a person answered, claim never confirmed;
    - ended_before_anyone_answered: call_stage is "" -- neither a menu nor a
      person was ever detected (e.g. the line played a carrier message and
      hung up after 7s before the agent's audio even connected, 2026-10-21);
    - call_stage None means the stage isn't known (the results file has
      moved on to another call) -- assume a person, as before.
    Used both when Emily ends the call herself and when it ends any other
    way (the rep hangs up, the silence timeout fires, the menu hangs up)."""
    if fields.get("claim_confirmed"):
        return "verified"
    if fields.get("callback_requested"):
        return "callback_requested"
    if call_stage == "phone_menu":
        return "ivr_dead_end_needs_callback"
    if call_stage == "":
        return "ended_before_anyone_answered"
    return "claim_not_confirmed"


def save_call_outcome_if_unset(outcome: str):
    """Set call_outcome only if nothing's recorded one yet, so a more specific
    outcome (e.g. "success") set earlier doesn't get clobbered by a later,
    less specific one (e.g. a status callback firing after the call already
    completed normally)."""
    if "call_outcome" not in _load_results():
        _save("call_outcome", outcome)


async def save_field(params: FunctionCallParams):
    field_name = params.arguments["field_name"]
    value = params.arguments["value"]
    # Defense in depth against the model re-calling this for a field it already
    # saved (observed: it repeated save_field for claim_confirmed 4 times in one
    # call off vague acknowledgments like "yeah"/"hello?"). The prompt now also
    # tells it not to, but this guard protects the stored data either way.
    if field_name in _load_results():
        logger.info(f"save_field: {field_name!r} already recorded, ignoring duplicate call")
        await params.result_callback({"status": "already_recorded", "field_name": field_name})
        return
    _save(field_name, value)
    await params.result_callback({"status": "saved", "field_name": field_name})


_DIGIT_WORDS = "zero one two three four five six seven eight nine".split()


def _read_back(value: str) -> str:
    """A phone/fax number as words, grouped the usual US way (3-3-4, or
    1-3-3-4 with a country code). The model was seen reading a saved
    10-digit fax back with an extra digit; this gives it the exact text."""
    digits = re.sub(r"[^0-9]", "", value)
    groups = [digits]
    if len(digits) == 10:
        groups = [digits[:3], digits[3:6], digits[6:]]
    elif len(digits) == 11:
        groups = [digits[0], digits[1:4], digits[4:7], digits[7:]]
    return ", ".join(" ".join(_DIGIT_WORDS[int(d)] for d in g) for g in groups)


def _phone_digit_issue(value: str) -> str | None:
    """Count digits in a phone/fax number -- a US number should have 10
    (local) or 11 (with country code) digits. Returns a description of
    what's missing if it's short, or None if it looks complete. Done here
    in code rather than trusted to the model's own counting, since LLMs
    are unreliable at precisely counting digits in a string (observed:
    numbers like "555423" and "333" getting saved as if complete)."""
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 10:
        return None
    missing = 10 - len(digits)
    return (
        f"only {len(digits)} digit(s) given ({digits!r}) -- a US number needs "
        f"10 or 11 digits total, so at least {missing} more are missing"
    )


async def save_adjuster_info(params: FunctionCallParams):
    existing = _load_results()
    incomplete_notes = []
    for key in ("name", "phone", "email", "fax"):
        value = params.arguments.get(key)
        if not value:
            continue
        if key in ("phone", "fax"):
            issue = _phone_digit_issue(value)
            if issue:
                incomplete_notes.append(f"{key}: {issue}")
                continue  # don't save an incomplete number
        field_name = f"adjuster_{key}"
        if existing.get(field_name, {}).get("value") == value:
            continue  # unchanged -- skip the redundant rewrite
        _save(field_name, value)
    if incomplete_notes:
        await params.result_callback(
            {
                "status": "partial",
                "issues": incomplete_notes,
                "instruction": "One or more numbers are incomplete -- ask the rep for "
                "the remaining digits before confirming those specific fields. Don't "
                "call this tool again with the same incomplete value, wait for the full number.",
            }
        )
    else:
        saved = _load_results()
        read_back = {
            key: _read_back(saved[f"adjuster_{key}"]["value"])
            for key in ("phone", "fax")
            if params.arguments.get(key) and f"adjuster_{key}" in saved
        }
        result = {"status": "saved"}
        if read_back:
            result["read_back"] = read_back
            result["instruction"] = (
                "When you repeat a phone or fax number back, say it exactly as in read_back."
            )
        await params.result_callback(result)


def make_save_field_schema(call_phase: dict | None = None) -> FunctionSchema:
    """The save_field tool. Given the call's phase, claim_confirmed is only
    accepted once the rep has replied after Emily gave the claim details.

    Observed 2026-10-21: in the same reply in which Emily read out the claim
    number, DOI and patient, the model also saved claim_confirmed -- before
    the rep had said anything. The rep then said they couldn't confirm it,
    but the call was already "Verified". The prompt says to save only what
    the rep confirms; this makes it impossible to save it any earlier.
    (call_phase["rep_replied_after_details"] is maintained by
    _make_turn_tracking_handlers.)"""

    async def handler(params: FunctionCallParams):
        if (
            call_phase is not None
            and params.arguments.get("field_name") == "claim_confirmed"
            and not call_phase.get("rep_replied_after_details")
        ):
            logger.warning(
                "save_field: claim_confirmed refused -- the rep hasn't replied since the "
                "claim details were given"
            )
            # run_llm=False: no extra model run for this result, so Emily
            # doesn't speak again before the rep answers.
            await params.result_callback(
                {
                    "status": "not_saved",
                    "reason": "Nothing is confirmed yet -- you have only just given the claim "
                    "details. Wait for the rep's answer and save it only if they confirm "
                    "the claim is on file and matches.",
                },
                properties=FunctionCallResultProperties(run_llm=False),
            )
            return
        await save_field(params)

    return FunctionSchema(
        name="save_field",
        description="Save a single confirmed piece of claim information collected from the insurance rep.",
        properties={
            "field_name": {
                "type": "string",
                "description": "One of: claim_confirmed, claim_status, rep_name, reference_number",
            },
            "value": {
                "type": "string",
                "description": "The confirmed value the rep gave for this field.",
            },
        },
        required=["field_name", "value"],
        handler=handler,
    )


save_field_schema = make_save_field_schema()  # no phase: no claim_confirmed ordering guard

save_adjuster_info_schema = FunctionSchema(
    name="save_adjuster_info",
    description="Save the adjuster's contact details in one call, with whichever pieces you have "
    "(name, phone, email, fax). Prefer collecting all four before calling this, but call it with "
    "whatever's confirmed if the rep can't provide everything.",
    properties={
        "name": {"type": "string", "description": "Adjuster's full name."},
        "phone": {"type": "string", "description": "Adjuster's direct phone number."},
        "email": {"type": "string", "description": "Adjuster's email address."},
        "fax": {"type": "string", "description": "Adjuster's fax number, for lien submission."},
    },
    required=[],
    handler=save_adjuster_info,
)


async def hang_up_twilio_call(call_sid: str):
    """Hang up a real Twilio call directly via the REST API. Shared by
    end_call and the IVR-stuck escalation -- both need a guaranteed hangup
    that doesn't depend on Pipecat's internal EndFrame/CancelFrame timing."""
    try:
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        client = TwilioClient(account_sid, auth_token)
        await asyncio.to_thread(lambda: client.calls(call_sid).update(status="completed"))
    except Exception as exc:  # noqa: BLE001 -- don't block ending our own side on this
        logger.warning(f"Failed to hang up Twilio call {call_sid}: {exc}")


# The rep must have replied at least this many times before Emily may hang up
# on a call where the claim was never confirmed. It was 3, but a rep can end
# the call at their first reply ("Not right now. Goodbye." -- seen
# 2026-10-21), and the model can no longer call tools during its opening
# (the reason for the guard), so one real reply is enough.
MIN_REP_TURNS_TO_END_UNCONFIRMED = 1


def make_end_call_schema(call_sid: str = "", call_phase: dict | None = None) -> FunctionSchema:
    """Build the end_call tool, bound to this specific call's call_sid via closure.

    Nothing currently tells the pipeline the call is over after Emily's
    goodbye -- it just sits open waiting indefinitely (until the idle-timeout
    eventually fires, 90s+ later). This gives the model an explicit way to
    end it right after saying goodbye: ends the Pipecat pipeline/websocket,
    and for a real Twilio call, also hangs up the actual PSTN call via the
    REST API as a safety net (in case the <Connect><Stream> fallthrough
    doesn't tear down the call on its own in every configuration).
    """

    phase = call_phase if call_phase is not None else {}

    async def end_call(params: FunctionCallParams):
        # Guard against hanging up too early. It used to refuse whenever the
        # claim wasn't confirmed -- which also meant a call where the claim
        # genuinely couldn't be found or didn't match could never end: Emily
        # said goodbye, was refused, and kept talking (even restarted her
        # opening) until the rep hung up. Observed 2026-10-19.
        # The model calling this on its first turn was the original reason
        # for the guard; that is now prevented structurally (no tools during
        # the opening line, see ContextTrimmer._maybe_enable_tools), so here
        # it's enough to require a real exchange before an unconfirmed ending.
        results = _load_results()
        confirmed = "claim_confirmed" in results
        rep_turns = phase.get("rep_turns", MIN_REP_TURNS_TO_END_UNCONFIRMED)
        if not confirmed and rep_turns < MIN_REP_TURNS_TO_END_UNCONFIRMED:
            logger.warning(
                f"end_call: refused -- claim not confirmed and the rep has only replied "
                f"{rep_turns} time(s)"
            )
            await params.result_callback(
                {
                    "status": "refused",
                    "reason": "Too early to end the call -- the conversation has barely "
                    "started. Don't repeat your opening; carry on with the next step.",
                }
            )
            return
        outcome = outcome_from_captured(captured_fields(results), "conversation")
        logger.info(f"end_call: ending the call ({outcome})")
        save_call_outcome_if_unset(outcome)
        phase["ended"] = True  # the goodbye auto-hang-up has nothing left to do
        await params.result_callback({"status": "ending_call"})
        if call_sid:
            await hang_up_twilio_call(call_sid)
        if params.worker_runner:
            await params.worker_runner.end()

    return FunctionSchema(
        name="end_call",
        description="End the phone call. Call this immediately after saying your closing "
        "goodbye, once there's nothing more to discuss -- the call does not end on its own.",
        properties={},
        required=[],
        handler=end_call,
    )


class ContextTrimmer(FrameProcessor):
    """Caps context sent to the LLM so per-turn latency stays flat as the call goes on.

    Keeps the system prompt, a running summary of everything older, and only
    the most recent messages -- always cutting at a user-turn boundary so a
    tool_call/tool-result pair never gets split. When the window overflows,
    the dropped messages are summarized in the background (a separate OpenAI
    call that doesn't block the live turn) instead of just being thrown
    away, so the agent keeps some memory of the earlier part of the call.
    (It used to be a Groq call, hence the groq package still in
    requirements.txt.)
    """

    def __init__(
        self,
        context: LLMContext,
        openai_api_key: str,
        max_history_messages: int = 16,
        summarizer_model: str = "gpt-6-luna",
        call_phase: dict | None = None,
        conversation_tools: list | None = None,
        timings: "CallTimings | None" = None,
        silence_watchdog: "SilenceWatchdog | None" = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._context = context
        self._timings = timings
        self._watchdog = silence_watchdog
        # Messages trimmed out of the context but not summarized yet. Used to
        # be summarized only if no summary was already running -- anything
        # trimmed meanwhile was lost for good, so later in a call the summary
        # stopped growing ("...asked to verify a patient. The user said
        # 'Okay.'" four minutes into a call, 2026-10-21).
        self._pending_summary: list = []
        # Shared with the IVR/conversation handlers in _build_pipeline -- see
        # _maybe_enable_tools.
        self._phase = call_phase if call_phase is not None else {"mode": "conversation"}
        self._conversation_tools = conversation_tools or []
        self._max = max_history_messages
        self._summary = ""
        self._summarizing = False
        self._summarizer_model = summarizer_model
        self._client = AsyncOpenAI(api_key=openai_api_key)

    def _trim(self, messages: list) -> list:
        if len(messages) <= 1:
            return messages
        system_msg, rest = messages[0], messages[1:]

        # _trim runs on every frame, many times per turn, and each run's
        # output feeds back in as the next run's input -- so any status line
        # we injected last time is still sitting in `rest`. Strip it before
        # adding a fresh one, or it duplicates endlessly (observed: 9+ copies
        # of the same "Already confirmed" line stacked up within one call).
        rest = [
            m
            for m in rest
            if not (
                m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and (
                    m["content"].startswith("Already confirmed")
                    or m["content"].startswith("Summary of the call so far")
                )
            )
        ]

        extra_system = []
        fields_status = _collected_fields_status()
        if fields_status:
            extra_system.append({"role": "system", "content": fields_status})
        if self._summary:
            extra_system.append(
                {"role": "system", "content": f"Summary of the call so far: {self._summary}"}
            )

        if len(rest) <= self._max:
            return [system_msg] + extra_system + rest

        cut = len(rest) - self._max
        while cut < len(rest) and rest[cut].get("role") != "user":
            cut += 1
        dropped, kept = rest[:cut], rest[cut:]

        if dropped:
            self._pending_summary.extend(dropped)
            self._maybe_start_summary()

        return [system_msg] + extra_system + kept

    def _maybe_start_summary(self):
        if self._pending_summary and not self._summarizing:
            batch, self._pending_summary = self._pending_summary, []
            self._summarizing = True  # set now, so the next frame can't start a second one
            asyncio.create_task(self._summarize(batch))

    async def _summarize(self, dropped_messages: list):
        lines = [
            f"{m['role']}: {m['content']}"
            for m in dropped_messages
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"]
        ]
        if not lines:
            self._summarizing = False
            self._maybe_start_summary()
            return

        self._summarizing = True
        try:
            resp = await self._client.chat.completions.create(
                model=self._summarizer_model,
                # gpt-6-luna only supports the default temperature (1) -- passing
                # 0 made every single summarization call fail silently (caught
                # below), so this has been a no-op since the model switch.
                # 60 was sometimes too few even for one sentence ("max_tokens
                # ... limit was reached" -> the summary was skipped).
                max_completion_tokens=150,
                messages=[
                    {
                        "role": "system",
                        "content": "Summarize this segment of a phone call in ONE short sentence. "
                        "Keep only concrete facts stated (names, numbers, confirmations). No preamble.",
                    },
                    {"role": "user", "content": "\n".join(lines)},
                ],
            )
            new_bit = (resp.choices[0].message.content or "").strip()
            if new_bit:
                self._summary = f"{self._summary} {new_bit}".strip()
        except Exception as exc:
            logger.warning(f"ContextTrimmer: background summarization failed: {exc}")
        finally:
            self._summarizing = False
            self._maybe_start_summary()  # whatever was trimmed meanwhile

    def _maybe_enable_tools(self):
        """Emily's opening line runs with NO tools, then tools come on for
        every later turn.

        With tools attached, the model was seen calling one in the same reply
        as its greeting -- save_field("claim_confirmed") before the rep had
        said a word, or end_call. Pipecat then runs the model again for the
        tool's result, before the greeting has made it into the context, so
        Emily said her whole opening a second time; with end_call it also
        left the turn stuck until the rep spoke again. Keeping tools off for
        that one reply removes both.

        Called for each LLMContextFrame, i.e. each time the model is about to
        run.

        Real calls: the opening is not an LLM run at all -- it's spoken
        straight through TTS (see _make_on_conversation_detected), with the
        phase going greeting_queued -> greeting_speaking. Tools come on when
        the greeting finishes playing (_on_bot_speaking), or at the latest
        here, before the first LLM run after it -- which can only be the
        rep's first reply, possibly spoken over the greeting.

        Mic demo: the opening is still the first LLM run ("opening"); the
        next one (after the reply) switches tools on before it goes."""
        mode = self._phase["mode"]
        if mode == "classifying":
            if self._timings:
                self._timings.mark("classification_requested")
        elif mode == "opening":
            self._phase["mode"] = "opening_sent"
            self._phase["greeted"] = True
        elif mode in ("opening_sent", "greeting_queued", "greeting_speaking"):
            self._enable_conversation_tools(f"LLM run after the opening ({mode})")

    def _enable_conversation_tools(self, why: str):
        self._context.set_tools(self._conversation_tools)
        self._phase["mode"] = "conversation"
        logger.debug(f"ContextTrimmer: tools enabled -- {why}")

    def _on_bot_speaking(self, frame):
        """Follow the spoken greeting: queued -> speaking -> finished. The
        output transport sends these frames upstream past this processor
        when audio actually starts and stops going out to the caller."""
        self._phase["bot_speaking"] = isinstance(frame, BotStartedSpeakingFrame)
        mode = self._phase["mode"]
        if isinstance(frame, BotStartedSpeakingFrame) and mode == "greeting_queued":
            self._phase["mode"] = "greeting_speaking"
        elif isinstance(frame, BotStoppedSpeakingFrame) and mode == "greeting_speaking":
            self._enable_conversation_tools("greeting finished playing")

    def _ensure_opening_in_context(self):
        """Safety net, once per greeting: before the first LLM run after the
        spoken opening, make sure the opening is in the context. Pipecat
        records a spoken TTSSpeakFrame itself (possibly cut short if the rep
        talked over it); only if that didn't happen is the line inserted --
        before the rep's latest turn, where it was actually spoken -- so the
        model never thinks it still has to greet."""
        line = self._phase.get("opening_line")
        if not line or self._phase.get("opening_verified"):
            return
        self._phase["opening_verified"] = True
        messages = self._context.get_messages()
        for m in messages:
            text = m.get("content") if isinstance(m.get("content"), str) else ""
            if m.get("role") == "assistant" and text.strip() and line.startswith(text.strip()[:15]):
                return
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=len(messages))
        logger.warning("ContextTrimmer: spoken opening wasn't in the context -- adding it")
        self._context.set_messages(
            messages[:last_user] + [{"role": "assistant", "content": line}] + messages[last_user:]
        )

    def _feed_watchdog(self, frame):
        w = self._watchdog
        if w is None:
            return
        if isinstance(frame, StartFrame):
            w.start()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            w.stop()
        elif isinstance(frame, UserStartedSpeakingFrame):
            w.activity(user_speaking=True)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            w.activity(user_speaking=False)
        elif isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
            w.activity()

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self._feed_watchdog(frame)
        self._context.transform_messages(self._trim)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            self._maybe_enable_tools()
            if self._phase["mode"] == "conversation":
                self._ensure_opening_in_context()
        elif isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
            self._on_bot_speaking(frame)
        await self.push_frame(frame, direction)


# The placeholder user turn the context starts with (see _build_pipeline).
CALL_CONNECTED_PLACEHOLDER = "(The call has just connected.)"
_CONTROL_TAG = re.compile(r"<\s*/?\s*(ivr|dtmf|mode)\b", re.IGNORECASE)


def _clean_conversation_history(history: list) -> list:
    """The navigator's saved messages, reduced to what was actually said:
    user and assistant text only, without the connect placeholder, tool
    plumbing, or IVR/classifier control tags (<ivr>, <dtmf>, <mode>), which
    must never reach Emily's conversation."""
    cleaned = []
    for message in history or []:
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if not content.strip() or content.strip() == CALL_CONNECTED_PLACEHOLDER:
            continue
        if _CONTROL_TAG.search(content):
            continue
        cleaned.append({"role": role, "content": content})
    return cleaned


def _make_on_conversation_detected(
    vad_start_strategy,
    system_prompt: str,
    call_phase: dict,
    opening_line: str = "",
    conversation_tools: list | None = None,
    timings: "CallTimings | None" = None,
):
    """Bind the call's state into the handler for "a person answered".

    The navigator's classifier request has already decided "human". Emily's
    opening is the fixed opening_line (build_opening_line), spoken straight
    through TTS -- no second LLM request to compose it. Pipecat appends a
    spoken TTSSpeakFrame to the context as an assistant turn itself, as it is
    actually spoken (partially, if the rep talks over it), so the next LLM
    run sees it and carries on with Turn 2.
    """

    async def _on_conversation_detected(processor, conversation_history: list):
        """A human picked up -- hand off from IVR-navigation mode to Emily's script."""
        # One greeting per leg, however many times this fires. Checked and set
        # before the first await, so two events can't both get past it (the
        # event loop doesn't switch tasks between these lines).
        if call_phase.get("greeted"):
            logger.info("IVR navigator: person detected again -- already greeted, ignoring")
            return
        call_phase["greeted"] = True
        if timings:
            timings.mark("human_detected")

        # A previous /ws leg of this same call may already have greeted the
        # person (call_results.json lives for the whole call).
        greeted_on_earlier_leg = _load_results().get("call_stage", {}).get("value") == "conversation"
        logger.info(
            "IVR navigator: live conversation detected -- "
            + ("already greeted on an earlier leg, resuming" if greeted_on_earlier_leg else "speaking the opening")
        )
        vad_start_strategy._enable_interruptions = True
        _save("call_stage", "conversation")

        messages = [{"role": "system", "content": system_prompt}]
        history = _clean_conversation_history(conversation_history)
        if greeted_on_earlier_leg:
            messages += [{"role": "assistant", "content": opening_line}] + history
            call_phase["mode"] = "conversation"
            await processor.push_frame(
                LLMSetToolsFrame(tools=conversation_tools or NOT_GIVEN), FrameDirection.UPSTREAM
            )
            await processor.push_frame(
                LLMMessagesUpdateFrame(messages=messages, run_llm=False), FrameDirection.UPSTREAM
            )
            return

        # No tools until the greeting is over (ContextTrimmer switches them on).
        call_phase["mode"] = "greeting_queued"
        call_phase["opening_line"] = opening_line  # for ContextTrimmer._ensure_opening_in_context
        await processor.push_frame(LLMSetToolsFrame(tools=NOT_GIVEN), FrameDirection.UPSTREAM)
        # Replace the classifier's context with Emily's prompt plus what the
        # person actually said -- without running the LLM.
        await processor.push_frame(
            LLMMessagesUpdateFrame(messages=messages + history, run_llm=False), FrameDirection.UPSTREAM
        )
        if timings:
            timings.mark("greeting_requested")
        await processor.push_frame(
            TTSSpeakFrame(text=opening_line, append_to_context=True), FrameDirection.DOWNSTREAM
        )

    return _on_conversation_detected


# Emily's spoken goodbye, and a rep's reply that only closes the call.
_GOODBYE = re.compile(
    r"\b(good\s?-?\s?bye|bye(-bye)?|have a (good|great|nice|wonderful|lovely) "
    r"(day|afternoon|evening|one|weekend))\b",
    re.IGNORECASE,
)
_CLOSING_REPLY = re.compile(
    r"^\W*((ok(ay)?|alright|sure|yes|yeah|thanks?( you)?|you too|same to you|"
    r"good\s?-?\s?bye|bye(-bye)?|take care|have a good (one|day))\W*)+$",
    re.IGNORECASE,
)
# The rep (or a recorded message) asking us to call another time. Matched in
# code rather than left to the model to save, since the model has already
# skipped tool calls it was told to make (end_call, 2026-10-21).
_CALLBACK_REQUEST = re.compile(
    # up to three words in between: "call me a little later" (2026-10-21)
    r"\b(call|ring|try)(\s+\w+){0,3}?\s+(back|again|later|tomorrow)\b"
    r"|\b(talk|speak)\s+(to|with)\s+you\s+(later|another time|tomorrow)\b"
    r"|\bcall(\s+\w+)?\s+(another|some other|a different)\s+(time|day)\b"
    r"|\b(another|some other|a different)\s+time\b"
    r"|\bnot\s+(right\s+)?now\b"
    r"|\b(can't|cannot|can not)\b.{0,40}\b(right now|today|at the moment)\b",
    re.IGNORECASE,
)

# When a sound with no words cuts Emily off, she asks this (CTO, 2026-10-22),
# then waits this long after it has finished playing for an answer before
# carrying on with what she was saying.
DID_YOU_SAY_TEXT = "Sorry, did you say something?"
DID_YOU_SAY_WAIT = 3.0  # seconds

# After Emily's goodbye has finished playing, how long the rep has to say
# something before the call is hung up.
GOODBYE_HANGUP_GRACE = 2.0  # seconds


def _mentions_claim(text: str, claim_context: dict) -> bool:
    """Whether text gives the claim's details: its claim number, or the
    patient's last name."""
    text = text or ""
    claim_digits = re.sub(r"\D", "", str(claim_context.get("claim_number", "")))
    if claim_digits and claim_digits in re.sub(r"\D", "", text):
        return True
    last_name = str(claim_context.get("patient_name", "")).strip().split(" ")[-1]
    return len(last_name) > 2 and last_name.lower() in text.lower()


def _make_turn_tracking_handlers(call_sid: str, call_phase: dict, claim_context: dict):
    """Event handlers that follow what Emily and the rep say, for two
    decisions that must not be left to the model:

    - claim_confirmed may only be saved once the rep has replied after
      Emily gave the claim details (see make_save_field_schema);
    - when Emily has said goodbye and the rep doesn't carry on, the call is
      hung up here. The prompt tells the model to call end_call with its
      goodbye, but on 2026-10-21 it said goodbye twice without calling it
      and both calls stayed open until hung up by hand.

    Returns (on_assistant_turn_stopped, on_user_turn_started,
    on_user_turn_stopped) for the context aggregators."""

    async def _hang_up_after_goodbye(token, grace: float):
        # Let the goodbye finish playing, then give the rep a moment.
        for _ in range(100):  # up to ~20s of speech
            if not call_phase.get("bot_speaking"):
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(grace)
        if call_phase.get("goodbye_token") is not token or call_phase.get("ended"):
            return  # the rep spoke, Emily said something newer, or end_call ran
        call_phase["ended"] = True
        outcome = outcome_from_captured(captured_fields(_load_results()), "conversation")
        save_call_outcome_if_unset(outcome)
        logger.info(f"Emily said goodbye and the rep didn't carry on -- hanging up ({outcome})")
        await hang_up_twilio_call(call_sid)

    def _arm(grace: float):
        token = object()
        call_phase["goodbye_token"] = token
        task = asyncio.create_task(_hang_up_after_goodbye(token, grace))
        call_phase.setdefault("_tasks", set()).add(task)
        task.add_done_callback(call_phase["_tasks"].discard)

    async def on_assistant_turn_stopped(aggregator, message):
        text = getattr(message, "content", None) or ""
        call_phase["emily_cut_off"] = bool(getattr(message, "interrupted", False))
        call_phase["goodbye_token"] = None  # anything Emily says replaces an earlier goodbye
        if _mentions_claim(text, claim_context):
            call_phase["claim_details_given"] = True
            call_phase["rep_replied_after_details"] = False
        if call_sid and call_phase.get("mode") == "conversation" and _GOODBYE.search(text):
            call_phase["goodbye_said"] = True
            _arm(GOODBYE_HANGUP_GRACE)

    async def on_user_turn_started(aggregator, *args):
        call_phase["goodbye_token"] = None  # the rep is still talking: don't hang up
        # Not cleared here: whether it was an answer is decided when the turn
        # ends (words -> answer; no words -> carry on).

    async def _ask_did_you_say(aggregator):
        token = object()
        call_phase["did_you_say_pending"] = token
        await aggregator.push_frame(TTSSpeakFrame(text=DID_YOU_SAY_TEXT, append_to_context=True))

        async def carry_on_if_no_answer():
            await asyncio.sleep(0.5)  # let the question start playing
            for _ in range(40):  # wait for it to finish (up to ~8s)
                if not call_phase.get("bot_speaking"):
                    break
                await asyncio.sleep(0.2)
            await asyncio.sleep(DID_YOU_SAY_WAIT)
            if call_phase.get("did_you_say_pending") is token and not call_phase.get("ended"):
                call_phase["did_you_say_pending"] = None
                logger.info("No answer to 'Did you say something?' -- carrying on")
                await aggregator.push_context_frame()

        task = asyncio.create_task(carry_on_if_no_answer())
        call_phase.setdefault("_tasks", set()).add(task)
        task.add_done_callback(call_phase["_tasks"].discard)

    async def on_user_turn_stopped(aggregator, strategy=None, message=None, *args):
        text = (getattr(message, "content", None) or "").strip()
        if not text:
            # Any sound stops Emily at once (CTO: as natural as possible), but
            # a sound that produced no English words -- a cough, line noise,
            # "mm", someone talking in the background in another language --
            # leaves nothing for the LLM to answer, and she used to stay
            # silent until the rep spoke again (55s once, 2026-10-21). Ask
            # "Did you say something?"; if nothing comes back, carry on.
            if call_phase.get("mode") != "conversation" or not aggregator:
                return
            if call_phase.get("did_you_say_pending"):
                # The answer to "Did you say something?" was another noise.
                call_phase["did_you_say_pending"] = None
                logger.info("Only noise after 'Did you say something?' -- carrying on")
                await aggregator.push_context_frame()
            elif call_phase.get("emily_cut_off"):
                call_phase["emily_cut_off"] = False
                logger.info("A sound with no words cut Emily off -- asking 'Did you say something?'")
                await _ask_did_you_say(aggregator)
            return
        call_phase["did_you_say_pending"] = None  # they said something: normal flow
        if call_phase.get("claim_details_given") or _mentions_claim(text, claim_context):
            call_phase["rep_replied_after_details"] = True
        if text and _CALLBACK_REQUEST.search(text) and "callback_requested" not in _load_results():
            _save("callback_requested", "yes")
            logger.info("Rep asked us to call back another time")
        # "Okay, bye" / "Thanks, you too" after Emily's goodbye just closes the call.
        if call_sid and call_phase.get("goodbye_said") and text and _CLOSING_REPLY.match(text):
            _arm(0.5)

    return on_assistant_turn_stopped, on_user_turn_started, on_user_turn_stopped


class CallTimings:
    """Startup latency marks for one /ws leg (monotonic clock).

    Only stage names, call/stream ids and durations are logged -- never
    transcripts, claim details or phone numbers. Each mark keeps its first
    occurrence. The summary is logged once, when the first audio of the
    greeting goes out to Twilio."""

    STAGES = (
        "pipeline_ready",
        "caller_speech_started",
        "first_final_transcript",
        "caller_turn_ended",
        "classification_requested",
        "human_detected",
        "greeting_requested",
        "first_tts_audio",
        "first_audio_out",
    )

    def __init__(self, call_sid: str = "", stream_sid: str = "", connected_at: float | None = None):
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.t0 = connected_at if connected_at is not None else time.monotonic()
        self.marks: dict[str, float] = {}
        self.reported = False

    def mark(self, name: str):
        self.marks.setdefault(name, time.monotonic())

    def _gap(self, start: str, end: str) -> str:
        if start == "stream_connected":
            begin = self.t0
        else:
            begin = self.marks.get(start)
        finish = self.marks.get(end)
        if begin is None or finish is None:
            return "n/a"
        return f"{finish - begin:.2f}s"

    def summary(self) -> str:
        parts = [
            ("stream->pipeline_ready", "stream_connected", "pipeline_ready"),
            ("stream->caller_speech", "stream_connected", "caller_speech_started"),
            ("caller_turn_end->classify_request", "caller_turn_ended", "classification_requested"),
            ("classify_llm", "classification_requested", "human_detected"),
            ("human->greeting_request", "human_detected", "greeting_requested"),
            ("tts_first_audio", "greeting_requested", "first_tts_audio"),
            ("transport", "first_tts_audio", "first_audio_out"),
            ("TOTAL caller_turn_end->first_audio", "caller_turn_ended", "first_audio_out"),
            ("TOTAL stream->first_audio", "stream_connected", "first_audio_out"),
        ]
        return " | ".join(f"{label} {self._gap(a, b)}" for label, a, b in parts)

    def maybe_report(self):
        if self.reported or "greeting_requested" not in self.marks or "first_audio_out" not in self.marks:
            return
        self.reported = True
        logger.info(
            f"[STARTUP TIMING] call={self.call_sid or '-'} stream={self.stream_sid or '-'} | {self.summary()}"
        )


class CallTimingObserver(BaseObserver):
    """Feeds CallTimings from frames moving through the pipeline. Watching
    with an observer (a Pipecat public API) keeps measurement out of the
    audio path itself."""

    def __init__(self, timings: CallTimings, **kwargs):
        super().__init__(**kwargs)
        self._timings = timings

    async def on_push_frame(self, data: FramePushed):
        frame, t = data.frame, self._timings
        if isinstance(frame, StartFrame) and isinstance(data.source, BaseOutputTransport):
            t.mark("pipeline_ready")
        elif isinstance(frame, TranscriptionFrame):
            t.mark("first_final_transcript")
        elif isinstance(frame, TTSAudioRawFrame) and "greeting_requested" in t.marks:
            t.mark("first_tts_audio")
        elif (
            isinstance(frame, BotStartedSpeakingFrame)
            and isinstance(data.source, BaseOutputTransport)
            and "greeting_requested" in t.marks
        ):
            t.mark("first_audio_out")
            t.maybe_report()


async def _dial_extension_via_call_redirect(call_sid: str, extension: str):
    """Send real DTMF using Twilio's own native <Play digits> mechanism.

    Confirmed via Twilio's docs: DTMF over bidirectional Media Streams is
    inbound-only (caller-pressed digits reach us as events) -- there is no
    supported way to send outbound DTMF through the stream itself. Playing a
    synthesized tone as in-band audio just relays it as ordinary voice-band
    sound; Twilio does not re-encode it into real DTMF signaling for the PSTN
    leg, so it's never guaranteed to be recognized by the far end's phone
    system (confirmed in testing: it wasn't, no matter how we tuned timing).

    The supported way is a TwiML redirect: update the call's live TwiML to
    <Play digits="..."> (Twilio's own infrastructure generates the real DTMF),
    then <Redirect> back to /twiml to reconnect our AI pipeline. This briefly
    disconnects our current websocket -- the caller of this function's pipeline
    run will end when that happens, and a fresh one starts when Twilio
    reconnects to /ws after the redirect.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    base_url = os.getenv("PUBLIC_BASE_URL")
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Play digits="{extension}#"/>'
        f"<Redirect>{base_url}/twiml</Redirect>"
        "</Response>"
    )
    client = TwilioClient(account_sid, auth_token)
    await asyncio.to_thread(lambda: client.calls(call_sid).update(twiml=twiml))


# How long after the call is answered to press a known extension. Measured
# by dialing the test line's extension by hand: pressing immediately on
# connect doesn't register, ~5s in does.
#
# On real calls Twilio presses it itself (extension_send_digits, passed as
# sendDigits when the call is placed). Waiting to recognize the phone menu
# first was always too late: the menu has to pause before there's a
# transcript to classify (~5.5s), the model takes ~1s, and Twilio takes
# another 2-3s to act on a redirect -- ~9s in total, while the test line's
# menu gives up after ~8s ("We did not receive an extension"). Logged on
# 2026-10-19. The cost: if a person answers instead of a menu, they hear the
# keypad tones before Emily speaks.
EXTENSION_DIAL_DELAY = 5.0  # seconds from the call being answered

# Twilio's sendDigits allows at most 32 characters; "w" is a 0.5s pause.
_SEND_DIGITS_MAX = 32


def extension_send_digits(extension: str) -> str:
    """Twilio sendDigits for a known extension: pause EXTENSION_DIAL_DELAY,
    then the extension and #. Returns "" if the extension has no dialable
    characters."""
    digits = re.sub(r"[^0-9*#]", "", extension or "")
    if not digits:
        return ""
    if not digits.endswith("#"):
        digits += "#"
    pauses = min(int(EXTENSION_DIAL_DELAY * 2), max(0, _SEND_DIGITS_MAX - len(digits)))
    return "w" * pauses + digits[:_SEND_DIGITS_MAX]


# What a phone menu says when it didn't take the extension. Matched against
# the menu's last two transcribed turns, since a sentence is often split
# across them ("...We did not" / "receive an extension").
_EXTENSION_REJECTED = re.compile(
    r"(did not|didn't|did n't|have not|haven't)\s+(receive|get|hear|recognize)"
    r"|not (a )?valid|invalid|not recognized",
    re.IGNORECASE,
)


def _make_extension_redial_handler(known_extension: str, call_sid: str, call_phase: dict):
    """Backup for a known extension on a real call: if the phone menu says it
    didn't receive it, press it again once, with real DTMF (the TwiML
    redirect). Twilio already pressed it once when the call connected."""
    redialed = False
    recent: list[str] = []

    async def on_user_turn_stopped(aggregator, strategy, message):
        nonlocal redialed
        if redialed or not known_extension or not call_sid or call_phase.get("mode") in (
            "opening",
            "opening_sent",
            "greeting_queued",
            "greeting_speaking",
            "conversation",
        ):
            return
        text = getattr(message, "content", None) or ""
        recent.append(text)
        del recent[:-2]
        if _EXTENSION_REJECTED.search(" ".join(recent)):
            redialed = True
            logger.info(
                f"Phone menu didn't take extension {known_extension!r} -- pressing it again "
                f"on call {call_sid} via Twilio's native mechanism"
            )
            await _dial_extension_via_call_redirect(call_sid, known_extension)

    return on_user_turn_stopped


def _make_on_ivr_status_changed(
    vad_start_strategy,
    known_extension: str = "",
    call_sid: str = "",
    call_phase: dict | None = None,
    leg_started_at: float | None = None,
):
    """Bind known_extension/call_sid/vad_start_strategy into the event handler
    via closure -- add_event_handler only passes (processor, status), so this
    is how we thread extra context through."""
    dialed = False
    call_phase = call_phase if call_phase is not None else {}
    leg_started_at = leg_started_at if leg_started_at is not None else time.monotonic()

    async def _on_ivr_status_changed(processor, status):
        nonlocal dialed
        logger.info(f"IVR navigator status: {status}")
        if status == IVRStatus.DETECTED:
            # Disable barge-in again in case this is a submenu reached after an
            # earlier human/conversation phase re-enabled it -- an automated
            # prompt looping shouldn't be able to self-interrupt like a person.
            vad_start_strategy._enable_interruptions = False
            # save_field/save_adjuster_info don't apply during IVR navigation --
            # left attached, the model sometimes calls them with junk values
            # instead of emitting <dtmf>/<ivr> tags. Clear them until a human
            # picks up (ContextTrimmer switches them back on after the opening).
            call_phase["mode"] = "ivr"
            await processor.push_frame(LLMSetToolsFrame(tools=NOT_GIVEN), FrameDirection.UPSTREAM)
            if _load_results().get("call_stage", {}).get("value") != "conversation":
                _save("call_stage", "phone_menu")
        # Real calls: Twilio already pressed the extension when the call was
        # answered (see EXTENSION_DIAL_DELAY); _make_extension_redial_handler
        # presses it again if the menu says it didn't get it. Only the local
        # mic demo dials here, on detection.
        if status == IVRStatus.DETECTED and known_extension and not dialed and not call_sid:
            dialed = True
            await asyncio.sleep(max(0.0, EXTENSION_DIAL_DELAY - (time.monotonic() - leg_started_at)))
            # No PSTN leg in the mic demo, so an in-band tone is fine for a by-ear check.
            logger.info(f"Dialing known extension {known_extension!r} now (mic demo, in-band tone)")
            digits_to_dial = known_extension + "#"
            for i, digit in enumerate(digits_to_dial):
                if i > 0:
                    await asyncio.sleep(0.25)
                await processor.push_frame(OutputDTMFUrgentFrame(button=KeypadEntry(digit)))

        if status == IVRStatus.STUCK:
            # Previously this just logged and the call sat there -- the
            # navigator had given up but nothing told the call to actually
            # stop. Mark it clearly for a human to retry manually, and end
            # the call rather than burning more time on a dead end.
            logger.warning(
                "IVR navigation stuck -- marking call for manual follow-up and ending it"
            )
            save_call_outcome_if_unset("ivr_dead_end_needs_callback")
            if call_sid:
                await hang_up_twilio_call(call_sid)
            await processor.push_frame(EndFrame())

    return _on_ivr_status_changed


# Silence check-ins (CTO, 2026-10-22: "Emily should speak after a
# deterministic time, let's say 1 minute, if she doesn't hear back -- 'Are
# you there?'"). Long silences are normal on these calls ("let me pull that
# up", holds), so this is generous. After SILENCE_MAX_CHECKINS unanswered
# check-ins the call is ended.
SILENCE_CHECKIN_SECONDS = float(os.getenv("SILENCE_CHECKIN_SECONDS", "60"))
SILENCE_MAX_CHECKINS = int(os.getenv("SILENCE_MAX_CHECKINS", "3"))
SILENCE_CHECKIN_TEXT = "Sorry, are you still there?"


class SilenceWatchdog:
    """Says "Sorry, are you still there?" after SILENCE_CHECKIN_SECONDS in
    which nobody has spoken, during the conversation with a person.

    Replaces Pipecat's user-idle timer, which only starts when Emily stops
    speaking while the rep isn't mid-turn: when a noise cut Emily off and no
    words followed, it never started, and Emily stayed silent for 55s until
    the rep spoke (2026-10-21). This one just measures time since anyone
    last spoke -- the rep starting or stopping, or Emily starting or
    stopping -- so every silence is covered, however it began. Fed by
    ContextTrimmer, which sees those frames go past."""

    def __init__(self, call_sid: str, call_phase: dict, speak, end_call):
        self._call_sid = call_sid
        self._phase = call_phase
        self._speak = speak  # async fn(text): speak a fixed line
        self._end_call = end_call  # async fn(): end the call
        self._last_activity = time.monotonic()
        self._user_speaking = False
        self._checkins = 0
        self._task: asyncio.Task | None = None

    def activity(self, *, user_speaking: bool | None = None):
        self._last_activity = time.monotonic()
        if user_speaking is not None:
            self._user_speaking = user_speaking
            if user_speaking:
                self._checkins = 0  # they're there

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _silent_for(self) -> float:
        return time.monotonic() - self._last_activity

    async def check(self):
        """One check; the loop runs this every half second."""
        if (
            self._phase.get("mode") != "conversation"
            or self._phase.get("bot_speaking")
            or self._user_speaking
            or self._phase.get("ended")
            or self._silent_for() < SILENCE_CHECKIN_SECONDS
        ):
            return
        self._checkins += 1
        self._last_activity = time.monotonic()
        if self._checkins <= SILENCE_MAX_CHECKINS:
            logger.info(
                f"No one has spoken for {SILENCE_CHECKIN_SECONDS:.0f}s -- checking in "
                f"({self._checkins}/{SILENCE_MAX_CHECKINS})"
            )
            await self._speak(SILENCE_CHECKIN_TEXT)
        else:
            logger.info(f"No answer to {SILENCE_MAX_CHECKINS} check-ins -- ending the call")
            await self._end_call()

    async def _run(self):
        try:
            while True:
                await asyncio.sleep(0.5)
                await self.check()
        except asyncio.CancelledError:
            pass


REQUIRED_ENV_VARS = ["DEEPGRAM_API_KEY", "OPENAI_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID"]


def _check_env():
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill them in."
        )


def _build_pipeline(
    transport,
    sample_rate: int,
    known_extension: str = "",
    enable_vad_interruptions: bool = True,
    call_sid: str = "",
    claim_context: dict | None = None,
    timings: CallTimings | None = None,
):
    """Build the agent's brain: VAD, STT, LLM, TTS, context, and the pipeline
    itself. The transport (local mic or Twilio WebSocket) is the only thing
    that differs between entry points -- everything else is shared."""
    timings = timings or CallTimings(call_sid)
    deepgram_key = os.getenv("DEEPGRAM_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    cartesia_key = os.getenv("CARTESIA_API_KEY")
    cartesia_voice_id = os.getenv("CARTESIA_VOICE_ID")

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())

    stt = DeepgramSTTService(api_key=deepgram_key, sample_rate=sample_rate)

    llm = OpenAILLMService(
        api_key=openai_key,
        # OpenAI's processing tier for the live conversation. "fast" (same as
        # "priority") cut the median time to Emily's first word from ~1.2s to
        # ~0.7s in a test on gpt-6-luna, at priority pricing. Set
        # OPENAI_SERVICE_TIER=default in .env to go back to standard
        # processing, or "auto" to use the project's setting.
        service_tier=os.getenv("OPENAI_SERVICE_TIER", "fast"),
        settings=OpenAILLMService.Settings(
            model="gpt-6-luna",
            temperature=0.3,
            max_completion_tokens=120,
            extra={"reasoning_effort": "none"},
        ),
    )

    tts = CartesiaTTSService(
        api_key=cartesia_key,
        voice_id=cartesia_voice_id,
        model="sonic-3.6",
        sample_rate=sample_rate,
    )

    leg_started_at = time.monotonic()
    system_prompt = _build_system_prompt(claim_context or DEFAULT_CLAIM_CONTEXT)
    opening_line = build_opening_line(claim_context or DEFAULT_CLAIM_CONTEXT)
    # Which stage the call is in, shared by the IVR/conversation handlers and
    # ContextTrimmer: "classifying" (who answered?), "ivr", "greeting_queued"
    # / "greeting_speaking" (the fixed opening line playing, no tools),
    # "conversation" (tools on). The mic demo has Emily speak first with an
    # LLM-written opening instead: "opening" / "opening_sent".
    # "greeted" makes the opening happen at most once per leg.
    call_phase = {
        "mode": "classifying" if call_sid else "opening",
        "rep_turns": 0,
        "greeted": False,
    }
    end_call_schema = make_end_call_schema(call_sid, call_phase)
    conversation_tools = [make_save_field_schema(call_phase), save_adjuster_info_schema, end_call_schema]
    # No tools until Emily has said her opening line to a person (see
    # ContextTrimmer._maybe_enable_tools) -- the IVR classifier also runs on
    # this context and must not be able to call them either.
    context = LLMContext(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": CALL_CONNECTED_PLACEHOLDER},
        ],
    )
    # Kept as a named variable (not inline) so IVR/conversation-mode handlers
    # below can flip _enable_interruptions on it at runtime -- it needs to
    # start off (an IVR's looping announcement shouldn't be able to interrupt
    # itself mid-dial) but switch back on the moment a real human is talking,
    # so normal barge-in still works for the actual conversation.
    vad_start_strategy = VADUserTurnStartStrategy(enable_interruptions=enable_vad_interruptions)
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[vad_start_strategy],
                # 0.5s was cutting people off mid-sentence on a normal breath/pause.
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=1.0)],
            ),
            # Pipecat's own idle timer is off (0): SilenceWatchdog does it.
            user_idle_timeout=0,
        ),
    )
    context_aggregator.user().add_event_handler(
        "on_user_turn_stopped", _make_extension_redial_handler(known_extension, call_sid, call_phase)
    )

    async def _count_rep_turns(aggregator, *args):
        # Replies from the person after Emily's opening -- see
        # MIN_REP_TURNS_TO_END_UNCONFIRMED.
        if call_phase["mode"] in ("opening_sent", "greeting_queued", "greeting_speaking", "conversation"):
            call_phase["rep_turns"] += 1

    context_aggregator.user().add_event_handler("on_user_turn_stopped", _count_rep_turns)

    async def _mark_turn_started(aggregator, *args):
        timings.mark("caller_speech_started")

    async def _mark_turn_stopped(aggregator, *args):
        timings.mark("caller_turn_ended")

    context_aggregator.user().add_event_handler("on_user_turn_started", _mark_turn_started)
    context_aggregator.user().add_event_handler("on_user_turn_stopped", _mark_turn_stopped)

    _assistant_stopped, _user_started, _user_stopped = _make_turn_tracking_handlers(
        call_sid, call_phase, claim_context or DEFAULT_CLAIM_CONTEXT
    )
    context_aggregator.assistant().add_event_handler("on_assistant_turn_stopped", _assistant_stopped)
    context_aggregator.user().add_event_handler("on_user_turn_started", _user_started)
    context_aggregator.user().add_event_handler("on_user_turn_stopped", _user_stopped)

    async def _watchdog_speak(text: str):
        await context_trimmer.push_frame(TTSSpeakFrame(text=text, append_to_context=True))

    async def _watchdog_end_call():
        call_phase["ended"] = True
        save_call_outcome_if_unset(outcome_from_captured(captured_fields(_load_results()), "conversation"))
        if call_sid:
            await hang_up_twilio_call(call_sid)
        else:
            await context_trimmer.push_frame(EndFrame())

    silence_watchdog = SilenceWatchdog(call_sid, call_phase, _watchdog_speak, _watchdog_end_call)
    context_trimmer = ContextTrimmer(
        context,
        openai_api_key=openai_key,
        max_history_messages=16,
        call_phase=call_phase,
        conversation_tools=conversation_tools,
        timings=timings,
        silence_watchdog=silence_watchdog,
    )

    ivr_navigator = IVRNavigator(llm=llm, ivr_prompt=_build_ivr_goal(known_extension))
    ivr_navigator.add_event_handler(
        "on_conversation_detected",
        _make_on_conversation_detected(
            vad_start_strategy, system_prompt, call_phase, opening_line, conversation_tools, timings
        ),
    )
    ivr_navigator.add_event_handler(
        "on_ivr_status_changed",
        _make_on_ivr_status_changed(
            vad_start_strategy, known_extension, call_sid, call_phase, leg_started_at
        ),
    )

    # DEBUG: log the raw LLM text before IVRProcessor strips <dtmf>/<ivr> tags out of
    # it, so we can see exactly what the model produced on each IVR-navigation turn.
    _ivr_processor = ivr_navigator._ivr_processor
    _orig_process_frame = _ivr_processor.process_frame

    async def _debug_ivr_process_frame(frame, direction):
        if isinstance(frame, LLMTextFrame):
            logger.debug(f"[IVR RAW LLM TEXT] {frame.text!r}")
        await _orig_process_frame(frame, direction)

    _ivr_processor.process_frame = _debug_ivr_process_frame

    pipeline = Pipeline(
        [
            transport.input(),
            vad,
            stt,
            context_aggregator.user(),
            context_trimmer,
            ivr_navigator,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    return PipelineWorker(
        pipeline,
        observers=[CallTimingObserver(timings)],
        params=PipelineParams(
            audio_in_sample_rate=sample_rate,
            audio_out_sample_rate=sample_rate,
            enable_metrics=True,
        ),
    )


async def _run_pipeline(task, on_runner_ready=None, speak_first: bool = True):
    runner = WorkerRunner()
    await runner.add_workers(task)

    if on_runner_ready:
        on_runner_ready(runner)

    if speak_first:
        # Kick the agent off -- it opens the call itself, per the script.
        # Only safe when we know a human is listening (mic demo). On a real
        # call, forcing this races the IVR classifier: Emily would blurt her
        # opening line before the navigator gets a chance to listen to
        # what's actually on the other end (an IVR menu vs a live person).
        await task.queue_frames([LLMRunFrame()])

    await runner.run()


async def run_call(on_runner_ready=None, known_extension: str = "", claim_context: dict | None = None):
    """Local mic/speaker entry point (no telephony)."""
    _check_env()

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        )
    )
    task = _build_pipeline(
        transport, sample_rate=16000, known_extension=known_extension, claim_context=claim_context
    )

    print("\n=== Voice agent running. Speak into your mic to play the insurance rep. Ctrl+C to stop. ===\n")
    await _run_pipeline(task, on_runner_ready=on_runner_ready)


async def run_call_twilio(
    websocket,
    stream_sid: str,
    call_sid: str,
    on_runner_ready=None,
    known_extension: str = "",
    claim_context: dict | None = None,
    stream_connected_at: float | None = None,
):
    """Real-phone-call entry point, driven by a Twilio Media Streams WebSocket.

    Args:
        websocket: The FastAPI WebSocket connection Twilio is streaming audio over.
        stream_sid: Twilio's stream identifier from the "start" event.
        call_sid: Twilio's call identifier, used for auto hang-up.
        known_extension: If the destination's extension is already known (e.g. from
            a spreadsheet column), the IVR navigator dials it immediately instead of
            guessing from the menu.
        claim_context: Per-call claim data (client_name, claim_number, doi,
            patient_name) -- e.g. one row from an uploaded Excel sheet. Falls
            back to DEFAULT_CLAIM_CONTEXT if not given.
    """
    _check_env()

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
        auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            add_wav_header=False,
            serializer=serializer,
        ),
    )
    # Twilio Media Streams run at 8kHz mulaw, phone-call quality.
    # VAD-triggered interruptions are off here: an IVR's recorded announcement
    # loops continuously, and treating that as "the user is talking, cancel
    # whatever we're doing" was killing our own DTMF output and in-flight LLM
    # calls every couple of seconds. Turn-taking still works via the speech-
    # timeout stop strategy below -- we just don't barge-in-cancel on a replay.
    task = _build_pipeline(
        transport,
        sample_rate=8000,
        known_extension=known_extension,
        enable_vad_interruptions=False,
        call_sid=call_sid,
        claim_context=claim_context,
        timings=CallTimings(call_sid, stream_sid, stream_connected_at),
    )

    # Don't force Emily to speak first here -- let the IVR classifier listen
    # to whatever plays first (IVR menu vs a live "hello") before deciding
    # how to respond. Her opening line still fires automatically once
    # on_conversation_detected confirms a human picked up.
    await _run_pipeline(task, on_runner_ready=on_runner_ready, speak_first=False)


async def main():
    clear_results()
    await run_call()


if __name__ == "__main__":
    asyncio.run(main())
