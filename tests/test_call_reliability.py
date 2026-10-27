"""Tests for call-reliability fixes found in live calls on 2026-10-21.

Run from the project root:  venv\\Scripts\\python -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402

CLAIM = {"client_name": "Acme Liens", "claim_number": "55608", "doi": "11/05/2000", "patient_name": "Jo Black"}


class ResultsFile(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = main.RESULTS_FILE
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        main.RESULTS_FILE = Path(path)

    def tearDown(self):
        main.clear_results()
        main.RESULTS_FILE = self._orig


class Msg:
    def __init__(self, content, interrupted=False):
        self.content = content
        self.interrupted = interrupted


class FakeUserAggregator:
    def __init__(self):
        self.context_pushes = 0
        self.spoken = []

    async def push_context_frame(self):
        self.context_pushes += 1

    async def push_frame(self, frame, *args):
        self.spoken.append(getattr(frame, "text", type(frame).__name__))


class NoiseInterruptionTests(ResultsFile):
    """CTO, 2026-10-22: a sound with no words that cuts Emily off -> she asks
    "Did you say something?"; if nothing comes back she carries on."""

    def setUp(self):
        super().setUp()
        self._orig_wait = main.DID_YOU_SAY_WAIT
        main.DID_YOU_SAY_WAIT = 0.1
        self.phase = {"mode": "conversation", "rep_turns": 2, "greeted": True, "bot_speaking": False}
        self.assistant_stopped, self.user_started, self.user_stopped = main._make_turn_tracking_handlers(
            "", self.phase, CLAIM
        )
        self.agg = FakeUserAggregator()

    def tearDown(self):
        main.DID_YOU_SAY_WAIT = self._orig_wait
        super().tearDown()

    async def test_noise_cut_off_asks_then_carries_on_if_no_answer(self):
        await self.assistant_stopped(None, Msg("I only caught eight digits for the fax, one", interrupted=True))
        await self.user_stopped(self.agg, None, Msg(None))  # no words transcribed
        self.assertEqual(self.agg.spoken, [main.DID_YOU_SAY_TEXT])
        self.assertEqual(self.agg.context_pushes, 0)
        await asyncio.sleep(1.0)  # no answer
        self.assertEqual(self.agg.context_pushes, 1)  # carries on with her workflow

    async def test_an_answer_to_the_question_takes_the_normal_path(self):
        await self.assistant_stopped(None, Msg("Could you give me the", interrupted=True))
        await self.user_stopped(self.agg, None, Msg(None))
        await self.user_started(self.agg)
        await self.user_stopped(self.agg, None, Msg("Yes, I said hold on a second."))
        await asyncio.sleep(1.0)
        self.assertEqual(self.agg.context_pushes, 0)  # Pipecat answers their words; no extra run

    async def test_another_noise_instead_of_an_answer_carries_on_without_asking_again(self):
        await self.assistant_stopped(None, Msg("Could you give me the", interrupted=True))
        await self.user_stopped(self.agg, None, Msg(None))
        await self.assistant_stopped(None, Msg("Sorry, did you", interrupted=True))  # noise cut the question
        await self.user_stopped(self.agg, None, Msg(None))
        self.assertEqual(self.agg.spoken, [main.DID_YOU_SAY_TEXT])  # asked once only
        self.assertEqual(self.agg.context_pushes, 1)
        await asyncio.sleep(1.0)
        self.assertEqual(self.agg.context_pushes, 1)  # and not a second time when the timer runs out

    async def test_real_words_when_cut_off_take_the_normal_path(self):
        await self.assistant_stopped(None, Msg("Could you give me the", interrupted=True))
        await self.user_stopped(self.agg, None, Msg("Sorry, go on."))
        self.assertEqual((self.agg.spoken, self.agg.context_pushes), ([], 0))

    async def test_noise_when_emily_finished_normally_does_nothing(self):
        await self.assistant_stopped(None, Msg("What's the claim status?", interrupted=False))
        await self.user_stopped(self.agg, None, Msg(None))
        self.assertEqual((self.agg.spoken, self.agg.context_pushes), ([], 0))

    async def test_not_in_a_phone_menu(self):
        self.phase["mode"] = "ivr"
        await self.assistant_stopped(None, Msg("one", interrupted=True))
        await self.user_stopped(self.agg, None, Msg(None))
        self.assertEqual((self.agg.spoken, self.agg.context_pushes), ([], 0))

    async def test_empty_turn_does_not_count_as_a_reply_to_the_claim_details(self):
        await self.assistant_stopped(None, Msg("I'm calling about claim 55608 for Jo Black."))
        await self.user_stopped(self.agg, None, Msg(None))
        self.assertFalse(self.phase.get("rep_replied_after_details"))
        await self.user_stopped(self.agg, None, Msg("Yes, that matches."))
        self.assertTrue(self.phase.get("rep_replied_after_details"))


class CoughInBurstsTests(NoiseInterruptionTests):
    """2026-10-22 log: a cough in two bursts. The second burst cut the
    question off; the carry-on timer fired while it was still going, so
    Emily repeated herself, then asked "did you say something?" again."""

    async def test_logged_sequence_now_asks_once_and_carries_on_once(self):
        await self.assistant_stopped(None, Msg("May I have your name and a call", interrupted=True))
        await self.user_stopped(self.agg, None, Msg(None))           # burst 1 ends, no words
        self.assertEqual(self.agg.spoken, [main.DID_YOU_SAY_TEXT])
        await self.user_started(self.agg)                            # burst 2 starts at once
        await self.assistant_stopped(None, Msg("Sorry, did", interrupted=True))
        await asyncio.sleep(1.0)                                     # longer than the wait
        self.assertEqual(self.agg.context_pushes, 0)                 # not while they're making sound
        await self.user_stopped(self.agg, None, Msg(None))           # burst 2 ends, no words
        self.assertEqual(self.agg.context_pushes, 1)                 # carries on once
        await asyncio.sleep(1.0)
        self.assertEqual((self.agg.spoken, self.agg.context_pushes), ([main.DID_YOU_SAY_TEXT], 1))

    async def test_another_cough_soon_after_just_carries_on(self):
        await self.assistant_stopped(None, Msg("Could you give me the", interrupted=True))
        await self.user_stopped(self.agg, None, Msg(None))
        await asyncio.sleep(1.0)                                     # no answer -> carried on
        self.assertEqual(self.agg.context_pushes, 1)
        await self.assistant_stopped(None, Msg("Could you give me the adjuster's", interrupted=True))
        await self.user_stopped(self.agg, None, Msg(None))           # cough again, within 20s
        self.assertEqual(self.agg.spoken, [main.DID_YOU_SAY_TEXT])   # not asked a second time
        self.assertEqual(self.agg.context_pushes, 2)


class EmailSpellingTests(ResultsFile):
    """2026-10-22: name spelled "Stuart", email saved as stewart.brown@..."""

    def test_alignment(self):
        a = main._align_email_to_name
        self.assertEqual(a("stewart.brown@mlm.com", "Stuart Brown"), "stuart.brown@mlm.com")
        self.assertEqual(a("stewartbrown@mlm.com", "Stuart Brown"), "stuartbrown@mlm.com")
        self.assertEqual(a("sbrown@mlm.com", "Stuart Brown"), "sbrown@mlm.com")          # initial kept
        self.assertEqual(a("mike.adams@x.com", "Michael Adams"), "mike.adams@x.com")     # nickname kept
        self.assertEqual(a("claims@insurer.com", "Stuart Brown"), "claims@insurer.com")  # not a name
        self.assertEqual(a("stuart.brown@stewart.com", "Stuart Brown"), "stuart.brown@stewart.com")  # domain untouched

    async def save(self, **args):
        class P:
            arguments = dict(args)
            result = None

            async def result_callback(self, result, **kw):
                self.result = result

        p = P()
        await main.save_adjuster_info(p)
        return p.result

    async def test_name_and_email_together(self):
        result = await self.save(name="Stuart Brown", email="stewart.brown@mlm.com")
        self.assertEqual(main._load_results()["adjuster_email"]["value"], "stuart.brown@mlm.com")
        self.assertIn("Read it back once", result["email_spelling"])

    async def test_email_given_after_the_name(self):
        await self.save(name="Stuart Brown")
        await self.save(email="stewart.brown@mlm.com")
        self.assertEqual(main._load_results()["adjuster_email"]["value"], "stuart.brown@mlm.com")

    async def test_name_given_after_the_email(self):
        await self.save(email="stewart.brown@mlm.com")
        await self.save(name="Stuart Brown")
        self.assertEqual(main._load_results()["adjuster_email"]["value"], "stuart.brown@mlm.com")

    async def test_matching_spelling_gets_no_note(self):
        result = await self.save(name="Stuart Brown", email="stuart.brown@mlm.com")
        self.assertNotIn("email_spelling", result)


class ReadBackTests(ResultsFile):
    def test_groups(self):
        self.assertEqual(main._read_back("1123345676"), "one one two, three three four, five six seven six")
        self.assertEqual(main._read_back("+1 (426) 789-1011"),
                         "one, four two six, seven eight nine, one zero one one")

    async def test_saved_numbers_come_back_with_exact_read_back(self):
        class P:
            arguments = {"name": "Stuart Brown", "phone": "111-222-1234", "fax": "1123345676"}
            result = None

            async def result_callback(self, result, **kw):
                self.result = result

        p = P()
        await main.save_adjuster_info(p)
        self.assertEqual(p.result["status"], "saved")
        self.assertEqual(p.result["read_back"]["fax"], "one one two, three three four, five six seven six")
        self.assertEqual(p.result["read_back"]["phone"], "one one one, two two two, one two three four")


class SummarizerTests(unittest.IsolatedAsyncioTestCase):
    """Messages trimmed while a summary was already running were lost."""

    async def test_messages_trimmed_during_a_running_summary_are_summarized_later(self):
        calls = []

        class FakeCompletions:
            async def create(self, **kw):
                calls.append(kw["messages"][1]["content"])
                await asyncio.sleep(0.05)
                return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": f"part{len(calls)}."})()})()]})()

        trimmer = main.ContextTrimmer(LLMContext(messages=[{"role": "system", "content": "S"}]),
                                      openai_api_key="test", max_history_messages=2)
        trimmer._client = type("Client", (), {"chat": type("Chat", (), {"completions": FakeCompletions()})()})()

        def convo(n):
            return [{"role": "system", "content": "S"}] + [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{n}-{i}"} for i in range(6)
            ]

        trimmer._trim(convo(1))  # starts summary #1
        trimmer._trim(convo(2))  # trimmed while #1 runs -- used to be lost
        await asyncio.sleep(0.4)
        self.assertEqual(len(calls), 2)
        self.assertIn("m2-0", calls[1])
        self.assertEqual(trimmer._summary, "part1. part2.")


class CtrlCTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_runner_does_not_take_over_ctrl_c(self):
        seen = {}

        class FakeRunner:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            async def add_workers(self, *a):
                pass

            async def run(self):
                pass

        orig = main.WorkerRunner
        main.WorkerRunner = FakeRunner
        try:
            await main._run_pipeline(object(), speak_first=False)
        finally:
            main.WorkerRunner = orig
        self.assertIs(seen.get("handle_sigint"), False)


class ClosingPromptTests(unittest.TestCase):
    def test_yes_is_not_treated_as_the_answer(self):
        prompt = main._build_system_prompt(CLAIM)
        self.assertIn('A reply like "yes", "sure", "okay" or "well..." to that question is NOT', prompt)
        self.assertNotIn("WHATEVER it is", prompt)


class SilenceWatchdogTests(unittest.IsolatedAsyncioTestCase):
    """CTO: if Emily hears nothing back for a set time (1 minute), she asks
    "are you still there?" -- however the silence started."""

    def setUp(self):
        self._orig = (main.SILENCE_CHECKIN_SECONDS, main.SILENCE_MAX_CHECKINS)
        main.SILENCE_CHECKIN_SECONDS, main.SILENCE_MAX_CHECKINS = 0.2, 2
        self.spoken, self.ended = [], []
        self.phase = {"mode": "conversation", "bot_speaking": False}

        async def speak(text):
            self.spoken.append(text)

        async def end_call():
            self.ended.append(True)

        self.w = main.SilenceWatchdog("CA_TEST", self.phase, speak, end_call)

    def tearDown(self):
        main.SILENCE_CHECKIN_SECONDS, main.SILENCE_MAX_CHECKINS = self._orig

    async def wait_and_check(self, seconds=0.25):
        await asyncio.sleep(seconds)
        await self.w.check()

    async def test_checks_in_after_the_set_silence_then_ends_the_call(self):
        await self.w.check()
        self.assertEqual(self.spoken, [])  # not silent long enough yet
        await self.wait_and_check()
        self.assertEqual(self.spoken, [main.SILENCE_CHECKIN_TEXT])
        await self.wait_and_check()
        self.assertEqual(len(self.spoken), 2)
        await self.wait_and_check()
        self.assertEqual(self.ended, [True])  # third silence: no answer to 2 check-ins

    async def test_silence_after_a_noise_cut_off_is_covered(self):
        # rep "starts" (noise) -> Emily stops -> rep "stops", no words: the case
        # Pipecat's own idle timer missed.
        self.w.activity(user_speaking=True)
        self.w.activity()  # Emily stopped speaking
        self.w.activity(user_speaking=False)
        await self.wait_and_check()
        self.assertEqual(self.spoken, [main.SILENCE_CHECKIN_TEXT])

    async def test_no_check_in_while_someone_is_speaking_or_outside_conversation(self):
        self.w.activity(user_speaking=True)
        await self.wait_and_check()
        self.w.activity(user_speaking=False)
        self.phase["bot_speaking"] = True
        await self.wait_and_check()
        self.phase["bot_speaking"] = False
        self.phase["mode"] = "ivr"
        await self.wait_and_check()
        self.assertEqual(self.spoken, [])

    async def test_rep_answering_resets_the_count(self):
        await self.wait_and_check()
        self.w.activity(user_speaking=True)  # "yes, still here"
        self.w.activity(user_speaking=False)
        await self.wait_and_check()
        await self.wait_and_check()
        self.assertEqual(len(self.spoken), 3)  # count restarted, so no hang-up yet
        self.assertEqual(self.ended, [])

    async def test_fed_by_the_real_trimmer(self):
        from pipecat.frames.frames import UserStartedSpeakingFrame, UserStoppedSpeakingFrame
        from pipecat.tests.utils import run_test

        trimmer = main.ContextTrimmer(LLMContext(messages=[{"role": "system", "content": "S"}]),
                                      openai_api_key="test", call_phase=self.phase, silence_watchdog=self.w)
        await run_test(trimmer, frames_to_send=[UserStartedSpeakingFrame()],
                       expected_down_frames=[UserStartedSpeakingFrame])
        self.assertTrue(self.w._user_speaking)
        await run_test(trimmer, frames_to_send=[UserStoppedSpeakingFrame()],
                       expected_down_frames=[UserStoppedSpeakingFrame])
        self.assertFalse(self.w._user_speaking)


if __name__ == "__main__":
    unittest.main()
