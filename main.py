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
    LLMMessagesUpdateFrame,
    LLMRunFrame,
    LLMSetToolsFrame,
    LLMTextFrame,
    OutputDTMFUrgentFrame,
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
Whenever the rep confirms a field, ALWAYS immediately call save_field (or
save_adjuster_info) with the exact value given -- this must happen every
single time a field is confirmed, don't wait until the end of the call, and
don't skip it. Saving the data correctly matters more than anything else in
this call. Confirming the value back to the rep (e.g. "Got it, XYZ-456789")
already happens naturally as part of the GOAL instructions below -- that's
enough, don't add any extra narration about writing or noting things down.
For the adjuster's contact details (field 3: name, phone, email, fax),
use save_adjuster_info with all the pieces you have together in ONE call --
don't call it separately for each piece, since the rep will usually give
these together.

HANDLING COMMON SITUATIONS
- If put on hold: say "Sure, I'll hold" and wait silently.
- If asked to repeat claim info: restate claim number, DOI, and patient name
  clearly, spelling out digits if asked.
- If the rep can't find the claim: double-check the DOI and offer to search
  by patient name instead of claim number.
- If interrupted mid-sentence: stop talking immediately and listen.

CLOSING
Once all fields are collected (or the rep indicates no more info is
available), do NOT repeat or summarize everything back -- that's already
confirmed field by field as you went. Just wrap up briefly: thank them for
their time, ask for their name and a call reference number for your records,
then say a short, warm goodbye.

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


def _save(field_name: str, value: str):
    results = _load_results()
    results[field_name] = {"value": value, "captured_at": datetime.now(timezone.utc).isoformat()}
    RESULTS_FILE.write_text(json.dumps(results, indent=2))
    print(f"\n[SAVED] {field_name} = {value}\n")


async def save_field(params: FunctionCallParams):
    field_name = params.arguments["field_name"]
    value = params.arguments["value"]
    _save(field_name, value)
    await params.result_callback({"status": "saved", "field_name": field_name})


async def save_adjuster_info(params: FunctionCallParams):
    for key in ("name", "phone", "email", "fax"):
        value = params.arguments.get(key)
        if value:
            _save(f"adjuster_{key}", value)
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

        summary_msg = (
            [{"role": "system", "content": f"Summary of the call so far: {self._summary}"}]
            if self._summary
            else []
        )

        if len(rest) <= self._max:
            return [system_msg] + summary_msg + rest

        cut = len(rest) - self._max
        while cut < len(rest) and rest[cut].get("role") != "user":
            cut += 1
        dropped, kept = rest[:cut], rest[cut:]

        if dropped and not self._summarizing:
            asyncio.create_task(self._summarize(dropped))

        return [system_msg] + summary_msg + kept

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
                temperature=0,
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


async def _on_conversation_detected(processor, conversation_history: list):
    """A human picked up -- hand off from IVR-navigation mode back to Emily's script."""
    logger.info("IVR navigator: live conversation detected, resuming normal script")
    # Tools were cleared on IVR detection (they don't belong in navigation mode,
    # and left attached the model would sometimes call save_field with junk
    # values instead of navigating) -- restore them now that a human is on the line.
    await processor.push_frame(
        LLMSetToolsFrame(tools=[save_field_schema, save_adjuster_info_schema]),
        FrameDirection.UPSTREAM,
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history
    await processor.push_frame(
        LLMMessagesUpdateFrame(messages=messages, run_llm=True),
        FrameDirection.UPSTREAM,
    )


def _make_on_ivr_status_changed(known_extension: str = ""):
    """Bind known_extension into the event handler via closure -- add_event_handler
    only passes (processor, status), so this is how we thread it through."""
    dialed = False

    async def _on_ivr_status_changed(processor, status):
        nonlocal dialed
        logger.info(f"IVR navigator status: {status}")
        if status == IVRStatus.DETECTED:
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
            logger.info(f"Dialing known extension {known_extension!r} now")
            # Many PBX/IVR systems wait for a "#" to terminate extension entry --
            # without it they just keep waiting for more digits until timeout,
            # which looks exactly like "we did not receive an extension".
            digits_to_dial = known_extension + "#"
            for i, digit in enumerate(digits_to_dial):
                if i > 0:
                    # IVR digit detectors need a gap between tones to register
                    # each press separately -- without it, repeated digits
                    # (e.g. "888") can blur into one tone or get dropped.
                    await asyncio.sleep(0.25)
                await processor.push_frame(OutputDTMFUrgentFrame(button=KeypadEntry(digit)))

    return _on_ivr_status_changed


REQUIRED_ENV_VARS = ["DEEPGRAM_API_KEY", "OPENAI_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID"]


def _check_env():
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill them in."
        )


def _build_pipeline(
    transport, sample_rate: int, known_extension: str = "", enable_vad_interruptions: bool = True
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

    context = LLMContext(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "(The call has just connected.)"},
        ],
        tools=[save_field_schema, save_adjuster_info_schema],
    )
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy(enable_interruptions=enable_vad_interruptions)],
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.5)],
            )
        ),
    )

    context_trimmer = ContextTrimmer(context, openai_api_key=openai_key, max_history_messages=16)

    ivr_navigator = IVRNavigator(llm=llm, ivr_prompt=_build_ivr_goal(known_extension))
    ivr_navigator.add_event_handler("on_conversation_detected", _on_conversation_detected)
    ivr_navigator.add_event_handler(
        "on_ivr_status_changed", _make_on_ivr_status_changed(known_extension)
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
        transport, sample_rate=8000, known_extension=known_extension, enable_vad_interruptions=False
    )

    # Don't force Emily to speak first here -- let the IVR classifier listen
    # to whatever plays first (IVR menu vs a live "hello") before deciding
    # how to respond. Her opening line still fires automatically once
    # on_conversation_detected confirms a human picked up.
    await _run_pipeline(task, on_runner_ready=on_runner_ready, speak_first=False)


async def main():
    clear_results()
    await run_call(known_extension="888")  # TEMP: testing the proactive-dial DTMF path via mic


if __name__ == "__main__":
    asyncio.run(main())
