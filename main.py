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
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
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
purposes. My name is Emily, and I'm calling on behalf of a provider, FMR.
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
Whenever the rep confirms a field, immediately call save_field with the
exact value given -- don't wait until the end of the call.
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


def _load_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {}


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

    Keeps the system prompt plus only the most recent messages, always cutting
    at a user-turn boundary so a tool_call/tool-result pair never gets split.
    """

    def __init__(self, context: LLMContext, max_history_messages: int = 16, **kwargs):
        super().__init__(**kwargs)
        self._context = context
        self._max = max_history_messages

    def _trim(self, messages: list) -> list:
        if len(messages) <= 1:
            return messages
        system_msg, rest = messages[0], messages[1:]
        if len(rest) <= self._max:
            return messages
        cut = len(rest) - self._max
        while cut < len(rest) and rest[cut].get("role") != "user":
            cut += 1
        return [system_msg] + rest[cut:]

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        self._context.transform_messages(self._trim)
        await self.push_frame(frame, direction)


async def main():
    deepgram_key = os.getenv("DEEPGRAM_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")
    cartesia_key = os.getenv("CARTESIA_API_KEY")
    cartesia_voice_id = os.getenv("CARTESIA_VOICE_ID")

    missing = [
        name
        for name, val in [
            ("DEEPGRAM_API_KEY", deepgram_key),
            ("GROQ_API_KEY", groq_key),
            ("CARTESIA_API_KEY", cartesia_key),
            ("CARTESIA_VOICE_ID", cartesia_voice_id),
        ]
        if not val
    ]
    if missing:
        raise SystemExit(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill them in."
        )

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        )
    )

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer())

    stt = DeepgramSTTService(api_key=deepgram_key)

    llm = GroqLLMService(
        api_key=groq_key,
        settings=GroqLLMService.Settings(
            model="qwen/qwen3.8-27b",
            temperature=0.3,
            max_completion_tokens=120,
        ),
    )

    tts = CartesiaTTSService(
        api_key=cartesia_key,
        voice_id=cartesia_voice_id,
        model="sonic-2",
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
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.5)]
            )
        ),
    )

    context_trimmer = ContextTrimmer(context, max_history_messages=16)

    pipeline = Pipeline(
        [
            transport.input(),
            vad,
            stt,
            context_aggregator.user(),
            context_trimmer,
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
            enable_metrics=True,
        ),
    )

    runner = WorkerRunner()
    await runner.add_workers(task)

    # Kick the agent off -- it opens the call itself, per the script.
    await task.queue_frames([LLMRunFrame()])

    print("\n=== Voice agent running. Speak into your mic to play the insurance rep. Ctrl+C to stop. ===\n")
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
