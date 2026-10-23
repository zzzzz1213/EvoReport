import unittest
import threading
from unittest.mock import patch
from urllib.error import URLError

from evoreport.llm import ArkReportWriter, ModelCallTimeout, call_with_deadline, _response_text, _response_usage


class ArkReportWriterTests(unittest.TestCase):
    def test_stuck_worker_keeps_its_slot_until_it_finishes(self):
        release = threading.Event()
        finished = threading.Event()
        second_called = threading.Event()
        slots = threading.BoundedSemaphore(1)

        def slow():
            release.wait(5)
            finished.set()

        try:
            with patch("evoreport.llm._model_call_slots", slots):
                with self.assertRaises(ModelCallTimeout):
                    call_with_deadline(slow, 0.05)
                with self.assertRaises(ModelCallTimeout):
                    call_with_deadline(second_called.set, 0.05)
                self.assertFalse(second_called.is_set())
                release.set()
                self.assertTrue(finished.wait(2))
                self.assertTrue(slots.acquire(timeout=2))
                slots.release()
        finally:
            release.set()

    def test_transport_timeout_is_reported_instead_of_falling_back(self):
        writer = ArkReportWriter("https://example.invalid", "demo", "test-key")
        for error in (TimeoutError(), URLError(TimeoutError())):
            with self.subTest(error=type(error).__name__), patch("evoreport.llm.urlopen", side_effect=error):
                with self.assertRaises(ModelCallTimeout):
                    writer.rewrite("草稿")

    def test_missing_api_key_keeps_local_draft(self):
        writer = ArkReportWriter("https://example.invalid/api/v3", "demo-model", "")
        result, used_llm, note = writer.rewrite("# 工作日报\n")
        self.assertEqual("# 工作日报\n", result)
        self.assertFalse(used_llm)
        self.assertIn("未配置", note)

    def test_extracts_responses_output_text(self):
        payload = {"output": [{"content": [{"type": "output_text", "text": "日报正文"}]}]}
        self.assertEqual("日报正文", _response_text(payload))

    def test_provider_usage_and_missing_usage_are_distinguished(self):
        measured = _response_usage({"usage": {"input_tokens": 12, "output_tokens": 4,
                                               "total_tokens": 16}}, "输入", "输出")
        estimated = _response_usage({}, "输入", "输出")
        self.assertEqual({"input_tokens": 12, "output_tokens": 4,
                          "total_tokens": 16, "source": "provider"}, measured)
        self.assertEqual("estimated", estimated["source"])
        self.assertEqual(estimated["input_tokens"] + estimated["output_tokens"],
                         estimated["total_tokens"])


if __name__ == "__main__":
    unittest.main()
