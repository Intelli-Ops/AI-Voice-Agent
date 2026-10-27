"""Tests for the direct opening greeting (no second LLM request).

Run from the project root:  venv\\Scripts\\python -m unittest discover -s tests -v

Covers: a person answering, an IVR answering, repeated detection events, a
person after an IVR, a person greeted on an earlier /ws leg, tool activation
around the greeting, the mic demo path, and the timing log. No network,
Twilio or LLM calls are made; call_results.json is redirected to a temp file.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from pipecat.extensions.ivr.ivr_navigator import IVRStatus  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMContextFrame,
    LLMMessagesUpdateFrame,
    LLMSetToolsFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from pipecat.tests.utils import run_test  # noqa: E402
from pipecat.utils.types import NotGiven  # noqa: E402

CLAIM = {"client_name": "Acme Liens", "claim_number": "55608", "doi": "11/05/2000", "patient_name": "Jo Black"}
TOOLS = [main.save_field_schema, main.save_adjuster_info_schema, main.make_end_call_schema("")]


class FakeProcessor:
    """Stands in for the navigator's IVRProcessor: records pushed frames."""

    def __init__(self):
        self.pushed = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append((frame, direction))

    def of(self, kind):
        return [(f, d) for f, d in self.pushed if isinstance(f, kind)]


class FakeVadStrategy:
    _enable_interruptions = False


def tool_names(context):
    return None if isinstance(context.tools, NotGiven) else [f.name for f in context.tools.standard_tools]


class ResultsFileMixin:
    def setUp(self):
        self._orig = main.RESULTS_FILE
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        main.RESULTS_FILE = Path(path)

    def tearDown(self):
        main.clear_results()
        main.RESULTS_FILE = self._orig


class OutcomeRuleTests(unittest.TestCase):
    def test_outcomes(self):
        o = main.outcome_from_captured
        self.assertEqual(o({"claim_confirmed": "yes"}, ""), "verified")
        self.assertEqual(o({}, "phone_menu"), "ivr_dead_end_needs_callback")
        self.assertEqual(o({}, "conversation"), "claim_not_confirmed")
        self.assertEqual(o({}, ""), "ended_before_anyone_answered")  # nothing ever detected
        self.assertEqual(o({}), "claim_not_confirmed")  # stage unknown: as before
        self.assertEqual(o({"callback_requested": "yes"}, "conversation"), "callback_requested")
        self.assertEqual(o({"callback_requested": "yes", "claim_confirmed": "yes"}, "conversation"), "verified")

    def test_callback_phrases(self):
        said = [
            "Sorry. I can't. Confirm it right now. Can you call again?",
            "Yeah. No. Not right now. Goodbye.",
            "Can you call back tomorrow?", "Please try again later.",
            "Call us another time.", "I can't do this today.",
            "And she can you call me a little later?", "I'll talk to you later.",
        ]
        not_said = ["Yes, I have it here.", "The claim is open.", "Hold on, let me check.", "Call me Sarah.",
                    "I don't see any other information for this patient here."]
        for t in said:
            self.assertTrue(main._CALLBACK_REQUEST.search(t), t)
        for t in not_said:
            self.assertFalse(main._CALLBACK_REQUEST.search(t), t)


class OpeningLineTests(unittest.TestCase):
    def test_uses_client_name_and_matches_prompt(self):
        line = main.build_opening_line(CLAIM)
        self.assertIn("calling on behalf of Acme Liens.", line)
        self.assertIn("This call may be recorded", line)
        self.assertTrue(line.endswith("Can you verify a patient for me?"))
        # The prompt quotes the exact same line, so the two can't drift apart.
        self.assertIn(f'"{line}"', main._build_system_prompt(CLAIM))

    def test_spreadsheet_text_is_made_speakable(self):
        line = main.build_opening_line({"client_name": "  Acme <ivr>wait</ivr>\n{Liens}  "})
        self.assertNotIn("<", line)
        self.assertNotIn("{", line)
        self.assertIn("Acme ivr wait /ivr Liens", line)

    def test_missing_client_name_falls_back(self):
        self.assertIn(main.DEFAULT_CLAIM_CONTEXT["client_name"], main.build_opening_line({}))


class HumanGreetingTests(ResultsFileMixin, unittest.IsolatedAsyncioTestCase):
    def make_handler(self, phase=None, timings=None):
        self.phase = phase or {"mode": "classifying", "rep_turns": 0, "greeted": False}
        self.vad = FakeVadStrategy()
        self.timings = timings or main.CallTimings("CA_TEST", "MZ_TEST")
        self.line = main.build_opening_line(CLAIM)
        return main._make_on_conversation_detected(
            self.vad, "SYSTEM PROMPT", self.phase, self.line, TOOLS, self.timings
        )

    async def test_direct_human_speaks_fixed_opening_without_llm_run(self):
        handler = self.make_handler()
        proc = FakeProcessor()
        await handler(proc, [{"role": "user", "content": "Hello?"}])

        speaks = proc.of(TTSSpeakFrame)
        self.assertEqual(len(speaks), 1)
        frame, direction = speaks[0]
        self.assertEqual(frame.text, self.line)
        self.assertTrue(frame.append_to_context)  # Pipecat records it as Emily's turn, as spoken
        self.assertEqual(direction, FrameDirection.DOWNSTREAM)  # towards TTS

        updates = proc.of(LLMMessagesUpdateFrame)
        self.assertEqual(len(updates), 1)
        update, up_dir = updates[0]
        self.assertFalse(update.run_llm)  # the whole point: no second LLM request
        self.assertEqual(up_dir, FrameDirection.UPSTREAM)
        self.assertEqual(
            update.messages,
            [{"role": "system", "content": "SYSTEM PROMPT"}, {"role": "user", "content": "Hello?"}],
        )

        tools, _ = proc.of(LLMSetToolsFrame)[0]
        self.assertIsInstance(tools.tools, NotGiven)  # no tools while the greeting plays
        self.assertEqual(self.phase["mode"], "greeting_queued")
        self.assertTrue(self.vad._enable_interruptions)
        self.assertEqual(main._load_results()["call_stage"]["value"], "conversation")
        self.assertIn("human_detected", self.timings.marks)
        self.assertIn("greeting_requested", self.timings.marks)

    async def test_repeated_detection_greets_once(self):
        handler = self.make_handler()
        proc = FakeProcessor()
        await handler(proc, [{"role": "user", "content": "Hello?"}])
        await handler(proc, [{"role": "user", "content": "Hello? Anyone there?"}])
        await handler(proc, [])
        self.assertEqual(len(proc.of(TTSSpeakFrame)), 1)
        self.assertEqual(len(proc.of(LLMMessagesUpdateFrame)), 1)

    async def test_history_is_cleaned_of_placeholder_and_control_tags(self):
        handler = self.make_handler()
        proc = FakeProcessor()
        history = [
            {"role": "user", "content": main.CALL_CONNECTED_PLACEHOLDER},
            {"role": "assistant", "content": "<ivr>wait</ivr>"},
            {"role": "assistant", "content": "<dtmf>2</dtmf>"},
            {"role": "user", "content": "Please hold while we transfer you."},
            {"role": "tool", "content": "{}"},
            {"role": "user", "content": "Claims, Sarah speaking."},
        ]
        await handler(proc, history)
        update, _ = proc.of(LLMMessagesUpdateFrame)[0]
        self.assertEqual(
            [m["content"] for m in update.messages[1:]],
            ["Please hold while we transfer you.", "Claims, Sarah speaking."],
        )

    async def test_person_already_greeted_on_earlier_leg_is_not_greeted_again(self):
        main._save("call_stage", "conversation")  # an earlier /ws leg of this call greeted
        handler = self.make_handler()
        proc = FakeProcessor()
        await handler(proc, [{"role": "user", "content": "Sorry, you cut out."}])
        self.assertEqual(proc.of(TTSSpeakFrame), [])
        update, _ = proc.of(LLMMessagesUpdateFrame)[0]
        self.assertFalse(update.run_llm)
        self.assertEqual(update.messages[1], {"role": "assistant", "content": self.line})
        tools, _ = proc.of(LLMSetToolsFrame)[0]
        self.assertEqual([f.name for f in tools.tools], ["save_field", "save_adjuster_info", "end_call"])
        self.assertEqual(self.phase["mode"], "conversation")


class IvrThenHumanTests(ResultsFileMixin, unittest.IsolatedAsyncioTestCase):
    async def test_ivr_detection_plays_no_greeting_then_human_gets_one(self):
        phase = {"mode": "classifying", "rep_turns": 0, "greeted": False}
        vad = FakeVadStrategy()
        ivr_handler = main._make_on_ivr_status_changed(vad, "888", "CA_TEST", phase)
        human_handler = main._make_on_conversation_detected(
            vad, "SYSTEM", phase, main.build_opening_line(CLAIM), TOOLS
        )
        proc = FakeProcessor()

        await ivr_handler(proc, IVRStatus.DETECTED)
        self.assertEqual(phase["mode"], "ivr")
        self.assertEqual(proc.of(TTSSpeakFrame), [])  # nothing spoken into the menu
        self.assertEqual(main._load_results()["call_stage"]["value"], "phone_menu")

        # Menu completes / transfers: COMPLETED alone is not "a person".
        await ivr_handler(proc, IVRStatus.COMPLETED)
        self.assertEqual(proc.of(TTSSpeakFrame), [])
        self.assertFalse(phase["greeted"])

        await human_handler(proc, [{"role": "user", "content": "Claims, this is Mike."}])
        await human_handler(proc, [{"role": "user", "content": "Hello?"}])
        self.assertEqual(len(proc.of(TTSSpeakFrame)), 1)
        self.assertEqual(main._load_results()["call_stage"]["value"], "conversation")


class ToolActivationTests(ResultsFileMixin, unittest.IsolatedAsyncioTestCase):
    def make_trimmer(self, mode):
        self.context = LLMContext(messages=[{"role": "system", "content": "x"}])
        self.phase = {"mode": mode, "rep_turns": 0, "greeted": mode != "classifying"}
        self.timings = main.CallTimings("CA_TEST")
        return main.ContextTrimmer(
            self.context, openai_api_key="test", call_phase=self.phase,
            conversation_tools=TOOLS, timings=self.timings,
        )

    async def test_no_tools_during_classification(self):
        trimmer = self.make_trimmer("classifying")
        await run_test(trimmer, frames_to_send=[LLMContextFrame(context=self.context)],
                       expected_down_frames=[LLMContextFrame])
        self.assertIsNone(tool_names(self.context))
        self.assertEqual(self.phase["mode"], "classifying")
        self.assertIn("classification_requested", self.timings.marks)

    async def test_tools_come_on_when_greeting_finishes_playing(self):
        trimmer = self.make_trimmer("greeting_queued")
        await run_test(
            trimmer,
            frames_to_send=[BotStartedSpeakingFrame()],
            frames_to_send_direction=FrameDirection.UPSTREAM,
            expected_up_frames=[BotStartedSpeakingFrame],
        )
        self.assertEqual(self.phase["mode"], "greeting_speaking")
        self.assertIsNone(tool_names(self.context))  # still none while she's speaking

        await run_test(
            trimmer,
            frames_to_send=[BotStoppedSpeakingFrame()],
            frames_to_send_direction=FrameDirection.UPSTREAM,
            expected_up_frames=[BotStoppedSpeakingFrame],
        )
        self.assertEqual(self.phase["mode"], "conversation")
        self.assertEqual(tool_names(self.context), ["save_field", "save_adjuster_info", "end_call"])

    async def test_rep_talking_over_greeting_gets_tools_on_first_llm_run(self):
        trimmer = self.make_trimmer("greeting_speaking")
        await run_test(trimmer, frames_to_send=[LLMContextFrame(context=self.context)],
                       expected_down_frames=[LLMContextFrame])
        self.assertEqual(self.phase["mode"], "conversation")
        self.assertEqual(tool_names(self.context), ["save_field", "save_adjuster_info", "end_call"])

    async def test_mic_demo_llm_opening_has_no_tools_then_tools(self):
        trimmer = self.make_trimmer("opening")
        await run_test(trimmer, frames_to_send=[LLMContextFrame(context=self.context)],
                       expected_down_frames=[LLMContextFrame])
        self.assertEqual(self.phase["mode"], "opening_sent")
        self.assertTrue(self.phase["greeted"])  # a later "person detected" can't greet again
        self.assertIsNone(tool_names(self.context))
        await run_test(trimmer, frames_to_send=[LLMContextFrame(context=self.context)],
                       expected_down_frames=[LLMContextFrame])
        self.assertEqual(tool_names(self.context), ["save_field", "save_adjuster_info", "end_call"])


class OpeningInContextTests(ResultsFileMixin, unittest.IsolatedAsyncioTestCase):
    def make(self, messages):
        self.line = main.build_opening_line(CLAIM)
        self.context = LLMContext(messages=messages)
        self.phase = {"mode": "greeting_speaking", "rep_turns": 0, "greeted": True, "opening_line": self.line}
        return main.ContextTrimmer(
            self.context, openai_api_key="test", call_phase=self.phase, conversation_tools=TOOLS
        )

    async def test_recorded_greeting_is_left_alone(self):
        trimmer = self.make([
            {"role": "system", "content": "S"}, {"role": "user", "content": "Hello?"},
            {"role": "assistant", "content": main.build_opening_line(CLAIM)},
            {"role": "user", "content": "Sure, go ahead."},
        ])
        await run_test(trimmer, frames_to_send=[LLMContextFrame(context=self.context)],
                       expected_down_frames=[LLMContextFrame])
        self.assertEqual(sum(m["role"] == "assistant" for m in self.context.get_messages()), 1)

    async def test_partly_spoken_greeting_counts_as_recorded(self):
        trimmer = self.make([
            {"role": "system", "content": "S"}, {"role": "user", "content": "Hello?"},
            {"role": "assistant", "content": "Hi, good afternoon! This call may"},
            {"role": "user", "content": "Who is this?"},
        ])
        await run_test(trimmer, frames_to_send=[LLMContextFrame(context=self.context)],
                       expected_down_frames=[LLMContextFrame])
        self.assertEqual(sum(m["role"] == "assistant" for m in self.context.get_messages()), 1)

    async def test_missing_greeting_is_inserted_before_the_reply(self):
        trimmer = self.make([
            {"role": "system", "content": "S"}, {"role": "user", "content": "Hello?"},
            {"role": "user", "content": "Sure, go ahead."},
        ])
        await run_test(trimmer, frames_to_send=[LLMContextFrame(context=self.context)],
                       expected_down_frames=[LLMContextFrame])
        self.assertEqual(
            [(m["role"], m["content"]) for m in self.context.get_messages() if m["role"] != "system"],
            [("user", "Hello?"), ("assistant", self.line), ("user", "Sure, go ahead.")],
        )


class MicDemoTests(ResultsFileMixin, unittest.IsolatedAsyncioTestCase):
    async def test_detection_after_llm_opening_does_not_greet_again(self):
        phase = {"mode": "opening_sent", "rep_turns": 0, "greeted": True}
        handler = main._make_on_conversation_detected(
            FakeVadStrategy(), "SYSTEM", phase, main.build_opening_line(CLAIM), TOOLS
        )
        proc = FakeProcessor()
        await handler(proc, [{"role": "user", "content": "Hi, yes?"}])
        self.assertEqual(proc.pushed, [])


class TimingTests(unittest.TestCase):
    def test_summary_has_stage_durations_and_no_call_content(self):
        t = main.CallTimings("CA_TEST", "MZ_TEST", connected_at=100.0)
        t.marks.update({
            "pipeline_ready": 101.0, "caller_speech_started": 101.5, "caller_turn_ended": 103.0,
            "classification_requested": 103.1, "human_detected": 103.9, "greeting_requested": 103.95,
            "first_tts_audio": 104.2, "first_audio_out": 104.3,
        })
        s = t.summary()
        self.assertIn("classify_llm 0.80s", s)
        self.assertIn("TOTAL caller_turn_end->first_audio 1.30s", s)
        self.assertIn("TOTAL stream->first_audio 4.30s", s)
        self.assertNotIn("Acme", s)

    def test_reports_once_only_after_greeting_audio(self):
        t = main.CallTimings("CA_TEST")
        t.mark("first_audio_out")
        t.maybe_report()
        self.assertFalse(t.reported)  # no greeting yet (e.g. IVR audio) -> no report
        t.mark("greeting_requested")
        t.maybe_report()
        self.assertTrue(t.reported)


class FakeParams:
    def __init__(self, arguments):
        self.arguments = arguments
        self.result = None
        self.properties = None
        self.worker_runner = None

    async def result_callback(self, result, *, properties=None):
        self.result, self.properties = result, properties


class Msg:
    def __init__(self, content):
        self.content = content


class ClaimConfirmedGuardTests(ResultsFileMixin, unittest.IsolatedAsyncioTestCase):
    """2026-10-21: claim_confirmed was saved in the same reply that read out
    the claim details, before the rep said anything."""

    def setUp(self):
        super().setUp()
        self.phase = {"mode": "conversation", "rep_turns": 1, "greeted": True}
        self.tool = main.make_save_field_schema(self.phase)
        self.assistant_stopped, self.user_started, self.user_stopped = main._make_turn_tracking_handlers(
            "", self.phase, CLAIM
        )

    async def save(self):
        p = FakeParams({"field_name": "claim_confirmed", "value": "yes"})
        await self.tool.handler(p)
        return p

    async def test_refused_in_the_same_reply_as_the_details(self):
        p = await self.save()  # model saves while still speaking the details
        self.assertEqual(p.result["status"], "not_saved")
        self.assertFalse(p.properties.run_llm)  # no extra model run, Emily waits
        self.assertNotIn("claim_confirmed", main._load_results())

    async def test_saved_once_the_rep_answers_the_details(self):
        await self.assistant_stopped(None, Msg("I'm calling about claim 55608, DOI 11/05/2000, for Jo Black."))
        p = await self.save()
        self.assertEqual(p.result["status"], "not_saved")  # rep hasn't answered yet
        await self.user_stopped(None, None, Msg("Yes, I have it here, it matches."))
        p = await self.save()
        self.assertEqual(p.result["status"], "saved")
        self.assertIn("claim_confirmed", main._load_results())

    async def test_other_fields_are_not_affected(self):
        p = FakeParams({"field_name": "claim_status", "value": "open"})
        await self.tool.handler(p)
        self.assertEqual(p.result["status"], "saved")


class GoodbyeHangUpTests(ResultsFileMixin, unittest.IsolatedAsyncioTestCase):
    """2026-10-21: Emily said goodbye without calling end_call; the call stayed open."""

    def setUp(self):
        super().setUp()
        self._orig_hang_up, self._orig_grace = main.hang_up_twilio_call, main.GOODBYE_HANGUP_GRACE
        self.hung_up = []

        async def fake_hang_up(sid):
            self.hung_up.append(sid)

        main.hang_up_twilio_call = fake_hang_up
        main.GOODBYE_HANGUP_GRACE = 0.05
        # The call has reached its normal end (closing question asked).
        self.phase = {"mode": "conversation", "rep_turns": 2, "greeted": True, "closing_asked": True}
        self.assistant_stopped, self.user_started, self.user_stopped = main._make_turn_tracking_handlers(
            "CA_TEST", self.phase, CLAIM
        )

    def tearDown(self):
        main.hang_up_twilio_call, main.GOODBYE_HANGUP_GRACE = self._orig_hang_up, self._orig_grace
        super().tearDown()

    async def settle(self):
        import asyncio
        await asyncio.sleep(0.8)

    async def test_goodbye_then_silence_hangs_up(self):
        await self.assistant_stopped(None, Msg("Thanks for your time. Goodbye."))
        await self.settle()
        self.assertEqual(self.hung_up, ["CA_TEST"])
        self.assertEqual(main._load_results()["call_outcome"]["value"], "claim_not_confirmed")

    async def test_waits_for_goodbye_to_finish_playing(self):
        import asyncio
        self.phase["bot_speaking"] = True
        await self.assistant_stopped(None, Msg("Have a good afternoon."))
        await asyncio.sleep(0.3)
        self.assertEqual(self.hung_up, [])  # still speaking
        self.phase["bot_speaking"] = False
        await self.settle()
        self.assertEqual(self.hung_up, ["CA_TEST"])

    async def test_rep_carrying_on_cancels_it(self):
        await self.assistant_stopped(None, Msg("Thanks for your time—goodbye."))
        await self.user_started(None)
        await self.user_stopped(None, None, Msg("Wait, one more thing about the fax number."))
        await self.settle()
        self.assertEqual(self.hung_up, [])

    async def test_rep_saying_bye_back_hangs_up(self):
        await self.assistant_stopped(None, Msg("Thanks, goodbye."))
        await self.user_started(None)
        await self.user_stopped(None, None, Msg("Okay, bye."))
        await self.settle()
        self.assertEqual(self.hung_up, ["CA_TEST"])

    async def test_no_hang_up_outside_conversation_or_without_goodbye(self):
        self.phase["mode"] = "greeting_speaking"
        await self.assistant_stopped(None, Msg("Goodbye."))
        self.phase["mode"] = "conversation"
        await self.assistant_stopped(None, Msg("Could I get the adjuster's name?"))
        await self.settle()
        self.assertEqual(self.hung_up, [])

    async def test_greeting_and_good_afternoon_are_not_goodbyes(self):
        # The opening says "Hi, good afternoon!" -- only "have a good afternoon"
        # (or goodbye/bye) counts, and even then only once in conversation mode.
        line = main.build_opening_line(CLAIM)
        self.assertIsNone(main._GOODBYE.search(line))
        for not_goodbye in ["Good afternoon to you too.", "Good afternoon, I'm calling about a claim.",
                            "Hello, good evening."]:
            self.assertIsNone(main._GOODBYE.search(not_goodbye), not_goodbye)
        for goodbye in ["Have a good afternoon.", "Thanks for your time—goodbye.", "Bye now.", "Have a great day!"]:
            self.assertIsNotNone(main._GOODBYE.search(goodbye), goodbye)
        # Even spoken while the phase has already become "conversation"
        # (greeting just finished playing), the opening doesn't hang up.
        self.phase["mode"] = "conversation"
        await self.assistant_stopped(None, Msg(line))
        await self.settle()
        self.assertEqual(self.hung_up, [])

    async def test_goodbye_after_stop_does_not_hang_up(self):
        # 2026-10-26: rep said "Stop." mid-questions; the model said goodbye.
        self.phase.pop("closing_asked")
        await self.user_stopped(None, None, Msg("Stop."))
        await self.assistant_stopped(None, Msg("Understood. Goodbye."))
        await self.settle()
        self.assertEqual(self.hung_up, [])

    async def test_goodbye_after_the_rep_ends_the_call_hangs_up(self):
        self.phase.pop("closing_asked")
        await self.user_stopped(None, None, Msg("Sorry, I can't give that out. Bye."))
        await self.assistant_stopped(None, Msg("No problem, thanks. Goodbye."))
        await self.settle()
        self.assertEqual(self.hung_up, ["CA_TEST"])

    async def test_closing_question_counts_as_the_end(self):
        self.phase.pop("closing_asked")
        await self.assistant_stopped(None, Msg("Thanks. Could I get your name and a call reference number?"))
        await self.user_stopped(None, None, Msg("No."))
        await self.assistant_stopped(None, Msg("No problem. Goodbye."))
        await self.settle()
        self.assertEqual(self.hung_up, ["CA_TEST"])

    async def test_end_call_tool_refused_without_an_ending(self):
        phase = {"mode": "conversation", "rep_turns": 3}
        tool = main.make_end_call_schema("CA_TEST", phase)

        class P:
            worker_runner = None
            result = properties = None

            async def result_callback(self, r, *, properties=None):
                self.result, self.properties = r, properties

        p = P()
        await tool.handler(p)
        self.assertEqual(p.result["status"], "refused")
        self.assertFalse(p.properties.run_llm)
        self.assertEqual(self.hung_up, [])
        phase["rep_signalled_end"] = True
        p = P()
        await tool.handler(p)
        self.assertEqual(p.result["status"], "ending_call")
        self.assertEqual(self.hung_up, ["CA_TEST"])

    async def test_nothing_twice_after_end_call(self):
        self.phase["ended"] = True  # end_call already hung up
        await self.assistant_stopped(None, Msg("Goodbye."))
        await self.settle()
        self.assertEqual(self.hung_up, [])


class GreetingThroughPipecatTests(unittest.IsolatedAsyncioTestCase):
    """The greeting through Pipecat's real TTS service base class (audio
    faked) and the real assistant context aggregator: it must be recorded
    in the context as Emily's turn exactly once, and must not ask the LLM to
    run."""

    async def test_spoken_greeting_is_recorded_once_and_runs_no_llm(self):
        from pipecat.frames.frames import TTSAudioRawFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
        from pipecat.services.tts_service import TTSService

        class FakeTTS(TTSService):
            async def run_tts(self, text, context_id):
                yield TTSAudioRawFrame(
                    audio=b"\x00\x00" * 800, sample_rate=8000, num_channels=1, context_id=context_id
                )

        line = main.build_opening_line(CLAIM)
        context = LLMContext(
            messages=[{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "Hello?"}]
        )
        pair = LLMContextAggregatorPair(context)
        # Same start/stop signalling as CartesiaTTSService (push_start_frame=True,
        # stop frames on), which is what lets the aggregator open and commit the turn.
        pipeline = Pipeline([FakeTTS(sample_rate=8000, push_start_frame=True, push_stop_frames=True), pair.assistant()])

        _down, up = await run_test(
            pipeline,
            frames_to_send=[TTSSpeakFrame(text=line, append_to_context=True)],
            expected_down_frames=None,
            expected_up_frames=None,
        )

        assistant_turns = [m for m in context.messages if m.get("role") == "assistant"]
        self.assertEqual(len(assistant_turns), 1)
        self.assertEqual(assistant_turns[0]["content"].strip(), line)
        # Order: the rep's "Hello?" stays before Emily's opening.
        self.assertEqual([m["role"] for m in context.messages], ["system", "user", "assistant"])
        # Nothing asked the LLM to run (that would be an LLMContextFrame/LLMRunFrame going up).
        from pipecat.frames.frames import LLMRunFrame
        self.assertFalse(any(isinstance(f, (LLMContextFrame, LLMRunFrame)) for f in up))


if __name__ == "__main__":
    unittest.main()
