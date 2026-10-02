"""
Local mic/speaker demo of the insurance-verification voice agent.

No telephony involved -- this talks to you directly through your PC's
microphone and speakers so the AI pipeline (STT -> LLM -> TTS, with
barge-in/interruption handling) can be validated before wiring up Telnyx.

Run: python main.py
Then just talk. Play the role of the insurance rep; the agent will ask
its questions and read them back for confirmation, same as it would on
a real call.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI

# DEBUG: verbose logging while we troubleshoot IVR/DTMF navigation -- shows
# the raw LLM text (including <dtmf>/<ivr> tags) so we can see whether the
# model is actually producing them or returning empty responses.
logger.remove()
logger.add(sys.stderr, level="DEBUG")

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.extensions.ivr.ivr_navigator import IVRNavigator, IVRStatus
from pipecat.frames.frames import (
    EndFrame,
    LLMMessagesUpdateFrame,
    LLMRunFrame,
    LLMSetToolsFrame,
    LLMTextFrame,
    OutputDTMFUrgentFrame,
    TTSSpeakFrame,
)
from pipecat.utils.types import NOT_GIVEN
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
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner
from twilio.rest import Client as TwilioClient

load_dotenv()

RESULTS_FILE = Path(__file__).parent / "call_results.json"

# Stand-in for the claim record you'd normally pull from your DB before
# placing the real call. Edit these to match whatever scenario you want
# to demo.
CLAIM_CONTEXT = {
    "client_name": "FMR",
    "claim_number": "12345",
    "doi": "11/11/2026",
    "patient_name": "Michael Anderson",
}

SYSTEM_PROMPT = f"""ROLE
Your name is Emily. You are calling on behalf of a medical lien management
company to verify claims information with an insurance adjuster's office,
for lien purposes. You are speaking with a representative or adjuster at
the insurance company.

OPENING
Turn 1: Say this in one natural line, then wait for their reply -- don't
pause mid-way, just say it and let them respond:
"Hi, good afternoon! This call may be recorded for quality and accuracy
purposes. My name is Emily, and I'm calling on behalf of FMR.
Can you verify a patient for me?"

Turn 2: Once they say yes/go ahead, give the claim details conversationally
(not as a rattled-off list) so they can pull up the file: mention the claim
number {CLAIM_CONTEXT["claim_number"]}, date of injury {CLAIM_CONTEXT["doi"]},
and patient name {CLAIM_CONTEXT["patient_name"]}.

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
Client: {CLAIM_CONTEXT["client_name"]}
Claim number: {CLAIM_CONTEXT["claim_number"]}
Date of injury (DOI): {CLAIM_CONTEXT["doi"]}
Patient / claimant name: {CLAIM_CONTEXT["patient_name"]}

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

HANDLING COMMON SITUATIONS
- If put on hold: say "Sure, I'll hold" and wait silently.
- If asked to repeat claim info: restate claim number, DOI, and patient name
  clearly, spelling out digits if asked.
- If the rep can't find the claim: double-check the DOI and offer to search
  by patient name instead of claim number.
- If interrupted mid-sentence: stop talking immediately and listen.

CLOSING -- this is TWO separate turns, not one:
Once all fields are collected (or the rep indicates no more info is
available), do NOT repeat or summarize everything back -- that's already
confirmed field by field as you went.

Turn A: Thank them for their time and ask for their name and a call
reference number for your records. Then STOP -- do not say goodbye yet,
wait for their actual reply.

Turn B: Only after they've answered (or told you they don't have a
reference number), say a short, warm goodbye, and in that SAME turn
immediately call the end_call tool. Never combine turn A and turn B into
one response -- saying goodbye before they've actually answered confuses
them and makes you sound broken.

NEVER call end_call anywhere else -- not after your opening line, not after
a single question, not just because a sentence felt complete. It is ONLY
for turn B above: after the literal spoken goodbye, at the very end of a
call where the claim has already been confirmed and there is truly nothing
left to ask.

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
the moment this system was detected -- do NOT dial it yourself, even if it's
mentioned again below. If the system says it didn't receive an extension or
asks again, that entry may still be processing -- respond with
`<ivr>wait</ivr>` rather than dialing anything."""
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


def clear_results():
    if RESULTS_FILE.exists():
        RESULTS_FILE.unlink()


def _collected_fields_status() -> str:
    """A deterministic "here's what's already confirmed" line, read straight
    from call_results.json. Used instead of trusting the LLM's own fuzzy
    one-sentence summary to remember which fields it already has -- that
    summary was observed losing adjuster info entirely (it only mentioned
    claim details) once the sliding window trimmed the actual exchange out,
    causing the model to re-ask for a name/phone it had already saved."""
    results = _load_results()
    if not results:
        return ""
    parts = [f"{field} = {entry['value']}" for field, entry in results.items()]
    return "Already confirmed, do NOT ask for these again: " + "; ".join(parts)


def _save(field_name: str, value: str):
    results = _load_results()
    results[field_name] = {"value": value, "captured_at": datetime.now(timezone.utc).isoformat()}
    RESULTS_FILE.write_text(json.dumps(results, indent=2))
    print(f"\n[SAVED] {field_name} = {value}\n")


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


async def save_adjuster_info(params: FunctionCallParams):
    existing = _load_results()
    for key in ("name", "phone", "email", "fax"):
        value = params.arguments.get(key)
        if not value:
            continue
        field_name = f"adjuster_{key}"
        if existing.get(field_name, {}).get("value") == value:
            continue  # unchanged -- skip the redundant rewrite
        _save(field_name, value)
    await params.result_callback({"status": "saved"})


save_field_schema = FunctionSchema(
    name="save_field",
    description="Save a single confirmed piece of claim information collected from the insurance rep.",
    properties={
        "field_name": {
            "type": "string",
            "description": "One of: claim_confirmed, claim_status",
        },
        "value": {
            "type": "string",
            "description": "The confirmed value the rep gave for this field.",
        },
    },
    required=["field_name", "value"],
    handler=save_field,
)

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


def make_end_call_schema(call_sid: str = "") -> FunctionSchema:
    """Build the end_call tool, bound to this specific call's call_sid via closure.

    Nothing currently tells the pipeline the call is over after Emily's
    goodbye -- it just sits open waiting indefinitely (until the idle-timeout
    eventually fires, 90s+ later). This gives the model an explicit way to
    end it right after saying goodbye: ends the Pipecat pipeline/websocket,
    and for a real Twilio call, also hangs up the actual PSTN call via the
    REST API as a safety net (in case the <Connect><Stream> fallthrough
    doesn't tear down the call on its own in every configuration).
    """

    async def end_call(params: FunctionCallParams):
        # Hard guard: observed the model call this on its very first turn,
        # right after the opening greeting, before the rep had said anything
        # -- hanging up the call before any real work happened. The prompt
        # alone wasn't a strong enough constraint for something this costly,
        # so refuse outright unless the claim has actually been confirmed.
        if "claim_confirmed" not in _load_results():
            logger.warning(
                "end_call: refused -- called before claim_confirmed was ever saved, "
                "the call isn't actually done yet"
            )
            await params.result_callback(
                {
                    "status": "refused",
                    "reason": "The call is not done -- claim confirmation hasn't happened "
                    "yet. Continue the conversation, do not end the call.",
                }
            )
            return
        await params.result_callback({"status": "ending_call"})
        if call_sid:
            try:
                account_sid = os.getenv("TWILIO_ACCOUNT_SID")
                auth_token = os.getenv("TWILIO_AUTH_TOKEN")
                client = TwilioClient(account_sid, auth_token)
                await asyncio.to_thread(lambda: client.calls(call_sid).update(status="completed"))
            except Exception as exc:  # noqa: BLE001 -- don't block ending our own side on this
                logger.warning(f"end_call: failed to hang up Twilio call {call_sid}: {exc}")
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
    the dropped messages are summarized in the background (a separate, cheap
    Groq call that doesn't block the live turn) instead of just being thrown
    away, so the agent keeps some memory of the earlier part of the call.
    """

    def __init__(
        self,
        context: LLMContext,
        openai_api_key: str,
        max_history_messages: int = 16,
        summarizer_model: str = "gpt-6-luna",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._context = context
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

        if dropped and not self._summarizing:
            asyncio.create_task(self._summarize(dropped))

        return [system_msg] + extra_system + kept

    async def _summarize(self, dropped_messages: list):
        lines = [
            f"{m['role']}: {m['content']}"
            for m in dropped_messages
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"]
        ]
        if not lines:
            return

        self._summarizing = True
        try:
            resp = await self._client.chat.completions.create(
                model=self._summarizer_model,
                # gpt-6-luna only supports the default temperature (1) -- passing
                # 0 made every single summarization call fail silently (caught
                # below), so this has been a no-op since the model switch.
                max_completion_tokens=60,
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

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self._context.transform_messages(self._trim)
        await self.push_frame(frame, direction)


def _make_on_conversation_detected(vad_start_strategy, end_call_schema):
    """Bind vad_start_strategy/end_call_schema into the handler via closure,
    so it can turn real barge-in back on now that a human (not a looping
    recording) is on the line -- see the comment on enable_vad_interruptions
    in _build_pipeline for why it starts disabled."""

    async def _on_conversation_detected(processor, conversation_history: list):
        """A human picked up -- hand off from IVR-navigation mode back to Emily's script."""
        logger.info("IVR navigator: live conversation detected, resuming normal script")
        vad_start_strategy._enable_interruptions = True
        # Tools were cleared on IVR detection (they don't belong in navigation mode,
        # and left attached the model would sometimes call save_field with junk
        # values instead of navigating) -- restore them now that a human is on the line.
        await processor.push_frame(
            LLMSetToolsFrame(
                tools=[save_field_schema, save_adjuster_info_schema, end_call_schema]
            ),
            FrameDirection.UPSTREAM,
        )
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history
        await processor.push_frame(
            LLMMessagesUpdateFrame(messages=messages, run_llm=True),
            FrameDirection.UPSTREAM,
        )

    return _on_conversation_detected


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


def _make_on_ivr_status_changed(vad_start_strategy, known_extension: str = "", call_sid: str = ""):
    """Bind known_extension/call_sid/vad_start_strategy into the event handler
    via closure -- add_event_handler only passes (processor, status), so this
    is how we thread extra context through."""
    dialed = False

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
            # picks up (on_conversation_detected restores them).
            await processor.push_frame(LLMSetToolsFrame(tools=NOT_GIVEN), FrameDirection.UPSTREAM)
        if status == IVRStatus.DETECTED and known_extension and not dialed:
            dialed = True
            logger.info(
                f"IVR detected -- waiting for the prompt to finish before dialing known "
                f"extension {known_extension!r} (dialing too early, while the system is "
                f"still playing its greeting, gets ignored even though the tones are sent)"
            )
            # Confirmed by manually dialing this same extension by hand: pressing
            # immediately on connect doesn't register, but pressing ~5s in does --
            # the system isn't listening for DTMF until its prompt audio finishes.
            await asyncio.sleep(5)

            if call_sid:
                # Real Twilio call -- use the native redirect mechanism, since
                # in-band audio tones over the Media Stream aren't reliably
                # recognized as real DTMF by the far end (see docstring above).
                logger.info(
                    f"Redirecting call {call_sid} to play real DTMF for extension "
                    f"{known_extension!r} via Twilio's native mechanism"
                )
                await _dial_extension_via_call_redirect(call_sid, known_extension)
            else:
                # Local mic demo -- no real telephony/PSTN leg to worry about,
                # so the in-band tone is fine for a by-ear sanity check.
                logger.info(f"Dialing known extension {known_extension!r} now (mic demo, in-band tone)")
                digits_to_dial = known_extension + "#"
                for i, digit in enumerate(digits_to_dial):
                    if i > 0:
                        await asyncio.sleep(0.25)
                    await processor.push_frame(OutputDTMFUrgentFrame(button=KeypadEntry(digit)))

    return _on_ivr_status_changed


IDLE_PROMPT_TIMEOUT = 30.0  # seconds of total silence before Emily checks in
IDLE_MAX_CHECKINS = 3  # consecutive silent check-ins (90s+ total) before giving up


def _make_idle_handlers():
    """Build the pair of event handlers for user-silence handling.

    Real insurance-rep calls routinely involve long silences the rep
    themselves asked for -- "let me pull that up," being on hold, searching
    a system. 15s-then-hangup (the first version of this) was ending real
    calls out from under genuine in-progress holds. Now: a gentle check-in
    every 30s of silence, up to 3 in a row (90s+ of total dead air with zero
    response even to being asked directly) before actually ending the call.
    Any real reply resets the counter back to zero.
    """
    idle_count = 0

    async def on_user_idle(aggregator):
        nonlocal idle_count
        idle_count += 1
        if idle_count < IDLE_MAX_CHECKINS:
            logger.info(f"No response for {IDLE_PROMPT_TIMEOUT}s -- checking in ({idle_count})")
            await aggregator.push_frame(TTSSpeakFrame(text="Sorry, are you still there?"))
        else:
            logger.info(f"No response after {idle_count} check-ins -- ending the call")
            await aggregator.push_frame(EndFrame())

    async def on_user_turn_stopped(aggregator, *args):
        nonlocal idle_count
        idle_count = 0

    return on_user_idle, on_user_turn_stopped


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
):
    """Build the agent's brain: VAD, STT, LLM, TTS, context, and the pipeline
    itself. The transport (local mic or Twilio WebSocket) is the only thing
    that differs between entry points -- everything else is shared."""
    deepgram_key = os.getenv("DEEPGRAM_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    cartesia_key = os.getenv("CARTESIA_API_KEY")
    cartesia_voice_id = os.getenv("CARTESIA_VOICE_ID")

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())

    stt = DeepgramSTTService(api_key=deepgram_key, sample_rate=sample_rate)

    llm = OpenAILLMService(
        api_key=openai_key,
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
        model="sonic-2",
        sample_rate=sample_rate,
    )

    end_call_schema = make_end_call_schema(call_sid)
    context = LLMContext(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "(The call has just connected.)"},
        ],
        tools=[save_field_schema, save_adjuster_info_schema, end_call_schema],
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
            user_idle_timeout=IDLE_PROMPT_TIMEOUT,
        ),
    )
    _on_user_idle, _on_user_turn_stopped_reset_idle = _make_idle_handlers()
    context_aggregator.user().add_event_handler("on_user_turn_idle", _on_user_idle)
    context_aggregator.user().add_event_handler(
        "on_user_turn_stopped", _on_user_turn_stopped_reset_idle
    )

    context_trimmer = ContextTrimmer(context, openai_api_key=openai_key, max_history_messages=16)

    ivr_navigator = IVRNavigator(llm=llm, ivr_prompt=_build_ivr_goal(known_extension))
    ivr_navigator.add_event_handler(
        "on_conversation_detected",
        _make_on_conversation_detected(vad_start_strategy, end_call_schema),
    )
    ivr_navigator.add_event_handler(
        "on_ivr_status_changed",
        _make_on_ivr_status_changed(vad_start_strategy, known_extension, call_sid),
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


async def run_call(on_runner_ready=None, known_extension: str = ""):
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
    task = _build_pipeline(transport, sample_rate=16000, known_extension=known_extension)

    print("\n=== Voice agent running. Speak into your mic to play the insurance rep. Ctrl+C to stop. ===\n")
    await _run_pipeline(task, on_runner_ready=on_runner_ready)


async def run_call_twilio(
    websocket, stream_sid: str, call_sid: str, on_runner_ready=None, known_extension: str = ""
):
    """Real-phone-call entry point, driven by a Twilio Media Streams WebSocket.

    Args:
        websocket: The FastAPI WebSocket connection Twilio is streaming audio over.
        stream_sid: Twilio's stream identifier from the "start" event.
        call_sid: Twilio's call identifier, used for auto hang-up.
        known_extension: If the destination's extension is already known (e.g. from
            a spreadsheet column), the IVR navigator dials it immediately instead of
            guessing from the menu.
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
