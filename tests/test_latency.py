"""Tests for the call-start latency changes (2026-10-26): the faster menu
model with a switch to gpt-6-luna for the conversation, smart turn
detection, and services built before the call connects.

Run from the project root:  venv\\Scripts\\python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import main  # noqa: E402
from pipecat.extensions.ivr.ivr_navigator import IVRStatus  # noqa: E402
from pipecat.frames.frames import ManuallySwitchServiceFrame, TTSSpeakFrame  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402


class FakeProcessor:
    def __init__(self):
        self.pushed = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.pushed.append((frame, direction))


class FakeVad:
    _enable_interruptions = False


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


class ModelSwitchTests(ResultsFile):
    """Menu model while classifying / in menus; gpt-6-luna once a person answers."""

    def setUp(self):
        super().setUp()
        self.menu, self.convo = object(), object()
        self.phase = {"mode": "classifying", "rep_turns": 0, "greeted": False,
                      "llms": {"menu": self.menu, "conversation": self.convo}}

    def switches(self, proc):
        return [(f.service, d) for f, d in proc.pushed if isinstance(f, ManuallySwitchServiceFrame)]

    async def test_person_detected_switches_to_gpt_before_greeting(self):
        handler = main._make_on_conversation_detected(FakeVad(), "SYS", self.phase, "Hi there.", [])
        proc = FakeProcessor()
        await handler(proc, [{"role": "user", "content": "Hello?"}])
        self.assertEqual(self.switches(proc), [(self.convo, FrameDirection.UPSTREAM)])
        kinds = [type(f).__name__ for f, _ in proc.pushed]
        self.assertLess(kinds.index("ManuallySwitchServiceFrame"), kinds.index("TTSSpeakFrame"))

    async def test_menu_detected_switches_to_menu_model(self):
        handler = main._make_on_ivr_status_changed(FakeVad(), "", "CA_TEST", self.phase)
        proc = FakeProcessor()
        await handler(proc, IVRStatus.DETECTED)
        self.assertEqual(self.switches(proc), [(self.menu, FrameDirection.UPSTREAM)])

    async def test_no_switch_frames_without_a_menu_model(self):
        self.phase["llms"] = None
        handler = main._make_on_conversation_detected(FakeVad(), "SYS", self.phase, "Hi there.", [])
        proc = FakeProcessor()
        await handler(proc, [])
        self.assertEqual(self.switches(proc), [])
        self.assertTrue(any(isinstance(f, TTSSpeakFrame) for f, _ in proc.pushed))


class ServicesTests(unittest.IsolatedAsyncioTestCase):
    def test_menu_model_only_with_an_openrouter_key(self):
        saved = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            self.assertIsNone(main._make_services(8000)["menu_llm"])
            os.environ["OPENROUTER_API_KEY"] = "test-key"
            llm = main._make_services(8000)["menu_llm"]
            self.assertEqual(llm._settings.model, main.MENU_LLM_MODEL)
            self.assertEqual(llm._settings.extra, {"extra_body": {"reasoning": {"enabled": False}}})
        finally:
            os.environ.pop("OPENROUTER_API_KEY", None)
            if saved is not None:
                os.environ["OPENROUTER_API_KEY"] = saved

    def test_turn_detection_setting(self):
        orig = main.TURN_DETECTION
        try:
            main.TURN_DETECTION = "smart"
            self.assertIsNotNone(main._make_services(8000)["smart_turn"])
            main.TURN_DETECTION = "timeout"  # the old fixed 1.0s rule
            self.assertIsNone(main._make_services(8000)["smart_turn"])
        finally:
            main.TURN_DETECTION = orig

    async def test_prewarm_take_discard(self):
        await main.prewarm_services("CA_PREWARM")
        self.assertIn("CA_PREWARM", main._PREWARMED)
        services = main.take_prewarmed("CA_PREWARM")
        self.assertIn("stt", services)
        self.assertIsNone(main.take_prewarmed("CA_PREWARM"))  # one leg only
        await main.prewarm_services("CA_UNUSED")
        main.discard_prewarmed("CA_UNUSED")  # e.g. no answer
        self.assertNotIn("CA_UNUSED", main._PREWARMED)

    def test_pipeline_uses_given_services_and_marks_timing(self):
        from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
        transport = LocalAudioTransport(LocalAudioTransportParams(audio_in_enabled=False, audio_out_enabled=False))
        timings = main.CallTimings("CA_TEST")
        main._build_pipeline(transport, 8000, call_sid="CA_TEST", claim_context=main.DEFAULT_CLAIM_CONTEXT,
                             timings=timings, services=main._make_services(8000))
        self.assertTrue(timings.prewarmed)


if __name__ == "__main__":
    unittest.main()
