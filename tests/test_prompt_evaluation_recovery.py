import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from evoreport.agent_runtime import MultiAgentRuntime
from evoreport.api import create_app
from evoreport.config import Settings
from evoreport.evolution import EvolutionService
from evoreport.store import Store
from tests.test_agent_instructions import PromptWriter, add_case


class InterruptWriter(PromptWriter):
    def __init__(self, fail_at, error=RuntimeError):
        super().__init__()
        self.fail_at = fail_at
        self.error = error

    def rewrite(self, draft, instruction=None):
        if len(self.calls) + 1 == self.fail_at:
            self.calls.append((draft, instruction))
            raise self.error("interrupted")
        return super().rewrite(draft, instruction)


class PromptEvaluationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "prompt.db"
        self.store = Store(self.path)
        for _ in range(3):
            add_case(self.store, self.path, "validation")
        add_case(self.store, self.path, "holdout")
        self.candidate = self.store.create_writer_prompt("表达简洁。")
        self.runtime = MultiAgentRuntime(self.store, PromptWriter())
        self.service = EvolutionService(self.store, self.runtime)
        self.service.create_benchmark()

    def test_validation_call_resumes_without_repeating_saved_scores(self):
        self.runtime.writer = InterruptWriter(3)
        failed = self.service.evaluate_writer_prompt(self.candidate["version"])
        self.assertEqual("error", failed["decision"])
        self.assertEqual("validation", failed["stage"])
        self.assertEqual(3, failed["model_calls"])
        self.assertEqual(2, sum(len(item.keys() & {"baseline", "candidate"})
                                for item in failed["progress"]["validation_cases"]))
        self.assertIsNone(self.store.list_evaluation_benchmarks()[0]["holdout_used_at"])
        self.assertEqual([failed["id"]], [item["id"] for item in
                         self.service.recoverable_writer_prompt_evaluations()])
        self.runtime.writer = PromptWriter()
        completed = self.service.resume_writer_prompt_evaluation(failed["id"])
        self.assertEqual("passed", completed["decision"])
        self.assertEqual(9, completed["model_calls"])
        self.assertEqual(6, len(self.runtime.writer.calls))
        self.assertEqual(3, len(completed["evidence"]["validation_cases"]))
        self.assertEqual(1, len(completed["evidence"]["holdout_cases"]))
        self.assertEqual(failed["id"], self.store.list_evaluation_benchmarks()[0]["holdout_run_id"])
        self.assertEqual([], self.service.recoverable_writer_prompt_evaluations())

    def test_timed_out_call_can_resume_and_late_result_is_ignored(self):
        release = threading.Event()
        finished = threading.Event()

        class SlowWriter(PromptWriter):
            def rewrite(self, draft, instruction=None):
                release.wait(5)
                finished.set()
                return "迟到结果", True, "测试"

        try:
            self.runtime.writer = SlowWriter()
            with patch("evoreport.evolution.MODEL_CALL_SECONDS", 0.05):
                failed = self.service.evaluate_writer_prompt(self.candidate["version"])
            self.assertEqual("error", failed["decision"])
            self.assertEqual("validation", failed["stage"])
            self.assertEqual(1, failed["model_calls"])
            self.assertEqual([failed["id"]], [item["id"] for item in
                             self.service.recoverable_writer_prompt_evaluations()])
            self.runtime.writer = PromptWriter()
            completed = self.service.resume_writer_prompt_evaluation(failed["id"])
            self.assertEqual("passed", completed["decision"])
            release.set()
            self.assertTrue(finished.wait(2))
            current = self.store.get_writer_prompt_evaluation(failed["id"])
            self.assertEqual("passed", current["decision"])
            self.assertEqual(completed["progress"], current["progress"])
            self.assertEqual(9, current["model_calls"])
        finally:
            release.set()

    def test_claimed_holdout_recovers_without_second_claim(self):
        self.runtime.writer = InterruptWriter(7)
        failed = self.service.evaluate_writer_prompt(self.candidate["version"])
        self.assertEqual("holdout_claimed", failed["stage"])
        self.assertEqual(failed["id"], self.store.list_evaluation_benchmarks()[0]["holdout_run_id"])
        self.runtime.writer = PromptWriter()
        with patch.object(self.store, "claim_benchmark_prompt_holdout", side_effect=AssertionError("claimed twice")):
            completed = self.service.resume_writer_prompt_evaluation(failed["id"])
        self.assertEqual("passed", completed["decision"])
        self.assertEqual(2, len(self.runtime.writer.calls))
        self.assertEqual(9, completed["model_calls"])

    def test_recovery_count_and_old_executor_fence(self):
        self.runtime.writer = InterruptWriter(1, KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.service.evaluate_writer_prompt(self.candidate["version"])
        running = self.store.list_writer_prompt_evaluations()[0]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE writer_prompt_evaluations SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (running["id"],),
            )
        self.runtime.writer = InterruptWriter(1)
        first = self.service.resume_writer_prompt_evaluation(running["id"])
        self.assertEqual("error", first["decision"])
        with self.assertRaisesRegex(ValueError, "接管"):
            self.store.save_writer_prompt_progress(
                running["id"], 0, "validation", running["progress"], 0,
            )
        self.runtime.writer = InterruptWriter(1)
        second = self.service.resume_writer_prompt_evaluation(running["id"])
        self.assertEqual(2, second["recovery_count"])
        self.assertEqual([], self.service.recoverable_writer_prompt_evaluations())
        with self.assertRaisesRegex(ValueError, "恢复预算"):
            self.service.resume_writer_prompt_evaluation(running["id"])

    def test_changed_sample_invalidates_resume_without_model_call(self):
        self.runtime.writer = InterruptWriter(2)
        failed = self.service.evaluate_writer_prompt(self.candidate["version"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            case_id = self.store.list_evaluation_benchmarks()[0]["validation_ids"][0]
            connection.execute(
                "UPDATE evaluation_cases SET expected_text='changed' WHERE id=?", (case_id,),
            )
        self.runtime.writer = PromptWriter()
        invalidated = self.service.resume_writer_prompt_evaluation(failed["id"])
        self.assertEqual("error", invalidated["decision"])
        self.assertEqual("invalidated", invalidated["stage"])
        self.assertEqual([], self.runtime.writer.calls)
        self.assertEqual([], self.service.recoverable_writer_prompt_evaluations())

    def test_call_budget_counts_failed_attempts(self):
        self.service.PROMPT_EVAL_MAX_CALLS = 2
        self.runtime.writer = InterruptWriter(1)
        first = self.service.evaluate_writer_prompt(self.candidate["version"])
        self.assertEqual(1, first["model_calls"])
        self.runtime.writer = InterruptWriter(1)
        exhausted = self.service.resume_writer_prompt_evaluation(first["id"])
        self.assertEqual("error", exhausted["decision"])
        self.assertEqual(2, exhausted["model_calls"])
        self.assertEqual("invalidated", exhausted["stage"])
        self.assertEqual([], self.service.recoverable_writer_prompt_evaluations())
        self.assertIsNone(self.store.list_evaluation_benchmarks()[0]["holdout_used_at"])

    def test_time_budget_stops_before_model_call(self):
        self.service.PROMPT_EVAL_SECONDS = 0
        self.runtime.writer = PromptWriter()
        exhausted = self.service.evaluate_writer_prompt(self.candidate["version"])
        self.assertEqual("error", exhausted["decision"])
        self.assertEqual("invalidated", exhausted["stage"])
        self.assertEqual(0, exhausted["model_calls"])
        self.assertEqual([], self.runtime.writer.calls)

    def test_changed_model_invalidates_resume(self):
        self.runtime.writer = InterruptWriter(1)
        self.runtime.writer.model = "model-a"
        failed = self.service.evaluate_writer_prompt(self.candidate["version"])
        self.runtime.writer = PromptWriter()
        self.runtime.writer.model = "model-b"
        invalidated = self.service.resume_writer_prompt_evaluation(failed["id"])
        self.assertEqual("invalidated", invalidated["stage"])
        self.assertEqual([], self.runtime.writer.calls)

    def test_completed_scores_survive_final_record_failure(self):
        with patch.object(self.store, "finish_writer_prompt_evaluation", side_effect=RuntimeError("disk error")):
            failed = self.service.evaluate_writer_prompt(self.candidate["version"])
        self.assertEqual("error", failed["decision"])
        self.assertEqual(8, failed["model_calls"])
        self.assertEqual("holdout_claimed", failed["stage"])
        self.runtime.writer = PromptWriter()
        completed = self.service.resume_writer_prompt_evaluation(failed["id"])
        self.assertEqual("passed", completed["decision"])
        self.assertEqual([], self.runtime.writer.calls)
        self.assertEqual(8, completed["model_calls"])

    def test_old_schema_is_migrated_without_marking_old_run_recoverable(self):
        old_path = Path(self.directory.name) / "old.db"
        with closing(sqlite3.connect(old_path)) as connection, connection:
            connection.execute("""CREATE TABLE writer_prompt_evaluations (
                id TEXT PRIMARY KEY,candidate_version INTEGER NOT NULL,baseline_version INTEGER NOT NULL,
                benchmark_version INTEGER NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,
                metrics_json TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at TEXT NOT NULL,
                completed_at TEXT,UNIQUE(candidate_version,benchmark_version)
            )""")
            connection.execute("""INSERT INTO writer_prompt_evaluations VALUES
                ('legacy',2,1,1,'error','old','{}','{}','2000-01-01',NULL)""")
        migrated = Store(old_path)
        self.assertEqual([], migrated.list_recoverable_writer_prompt_evaluations(60, 2, 10, 120000))
        self.assertEqual("legacy", migrated.get_writer_prompt_evaluation("legacy")["stage"])

    def test_api_lists_and_resumes_failed_evaluation(self):
        client = TestClient(create_app(Settings("127.0.0.1", 8090, self.path)))
        client.app.state.runtime.writer = InterruptWriter(7)
        failed = client.post(
            f"/api/agents/writer-prompts/{self.candidate['version']}/evaluate",
        ).json()
        self.assertEqual("error", failed["decision"])
        recoverable = client.get("/api/agents/recoverable").json()["runs"]
        self.assertEqual([failed["id"]], [item["id"] for item in recoverable])
        client.app.state.runtime.writer = PromptWriter()
        response = client.post(
            f"/api/agents/writer-prompts/evaluations/{failed['id']}/resume",
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("passed", response.json()["decision"])


if __name__ == "__main__":
    unittest.main()
