import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from evoreport.agent_runtime import MultiAgentRuntime
from evoreport.agent_runtime import RunBudgetExceeded
from evoreport.api import create_app
from evoreport.config import Settings
from evoreport.llm import ArkReportWriter
from evoreport.llm import ModelCallTimeout
from evoreport.role_executor import RoleProcessExecutor
from evoreport.store import Store
from evoreport import agent_runtime


DRAFT = "# 工作日报\n\n## 今日完成\n- 完成接口联调\n\n## 遇到的问题\n- 暂无\n\n## 明日计划\n- 补充测试\n"
SOURCE = {"kind": "daily", "facts": ["完成接口联调"], "projects": []}


class Writer:
    def __init__(self, failures=0):
        self.calls = 0
        self.failures = failures

    def rewrite(self, draft):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("writer unavailable")
        return draft, False, "测试写作"


class NeedsRepairWriter:
    def rewrite(self, draft):
        return "## 今日完成\n- 完成虚构发布\n## 遇到的问题\n", False, "测试缺陷草稿"


class InterceptingRoleExecutor:
    def __init__(self, intercept):
        self.delegate = RoleProcessExecutor()
        self.intercept = intercept

    def execute(self, role, operation, payload, timeout_seconds=None):
        self.intercept(role, operation, payload)
        return self.delegate.execute(role, operation, payload, timeout_seconds)

    def close(self):
        self.delegate.close()


def age_run(store, run_id):
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE agent_runs SET updated_at=? WHERE id=?", (old_time, run_id))


class AgentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "recovery.db")
        self.report = self.store.create_report("daily", "2026-10-14", SOURCE, DRAFT, 1)

    def test_daily_recovery_is_claimed_only_after_queue_admission(self):
        writer = Writer(failures=1)
        runtime = MultiAgentRuntime(self.store, writer)
        self.addCleanup(runtime.role_executor.close)
        with self.assertRaises(RuntimeError):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run_id = self.store.list_agent_runs(1)[0]["id"]
        results = []
        errors = []
        def resume():
            try:
                results.append(runtime.resume_report(run_id))
            except Exception as exc:
                errors.append(exc)
        with runtime.daily_queue.acquire(5):
            thread = threading.Thread(target=resume)
            thread.start()
            deadline = time.monotonic() + 3
            while runtime.daily_queue.status()["waiting"] != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(1, runtime.daily_queue.status()["waiting"])
            self.assertEqual("failed", self.store.get_agent_run(run_id)["status"])
            self.assertEqual(0, self.store.get_agent_run(run_id)["recovery_count"])
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(run_id, results[0]["run_id"])
        self.assertEqual(1, self.store.get_agent_run(run_id)["recovery_count"])

    def test_model_usage_is_recorded_without_report_body(self):
        writer = ArkReportWriter("https://example.invalid", "demo", "test-key")
        usage = {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100, "source": "provider"}
        with patch.object(ArkReportWriter, "rewrite_with_usage", return_value=(DRAFT, True, "测试", usage)) as call:
            result = MultiAgentRuntime(self.store, writer).run_report(self.report["id"], SOURCE, DRAFT)
        run = self.store.get_agent_run(result["run_id"])
        self.assertEqual(100, run["token_used"])
        self.assertEqual(0, run["token_estimated"])
        self.assertGreater(call.call_args.kwargs["max_output_tokens"], 0)
        step = next(item for item in run["steps"] if item["agent_name"] == "Writer Agent")
        self.assertEqual(usage, step["metrics"]["token_usage"])
        self.assertNotIn("完成接口联调", str(step))

    def test_model_deadline_preserves_checkpoint_and_discards_late_result(self):
        writer = ArkReportWriter("https://example.invalid", "demo", "test-key")
        runtime = MultiAgentRuntime(self.store, writer)
        release = threading.Event()
        finished = threading.Event()

        def slow(*args, **kwargs):
            release.wait(5)
            finished.set()
            return "迟到结果", True, "测试", None

        try:
            with patch("evoreport.agent_runtime.MODEL_CALL_SECONDS", 0.05), \
                 patch.object(ArkReportWriter, "rewrite_with_usage", side_effect=slow):
                with self.assertRaises(ModelCallTimeout):
                    runtime.run_report(self.report["id"], SOURCE, DRAFT)
            failed = self.store.list_agent_runs(1)[0]
            self.assertEqual("failed", failed["status"])
            self.assertEqual("ModelCallTimeout", failed["error_code"])
            self.assertEqual("facts_ready", self.store.get_agent_checkpoint(failed["id"])["stage"])
            self.assertGreater(failed["token_used"], 0)
            with patch.object(ArkReportWriter, "rewrite_with_usage", return_value=(DRAFT, True, "测试", None)):
                runtime.resume_report(failed["id"])
            release.set()
            self.assertTrue(finished.wait(2))
            completed = self.store.get_agent_run(failed["id"])
            self.assertEqual("passed", completed["status"])
            self.assertEqual(1, completed["recovery_count"])
            self.assertEqual(DRAFT, self.store.get_report(self.report["id"])["draft"])
            self.assertEqual(1, sum(step["agent_name"] == "Writer Agent" for step in completed["steps"]))
        finally:
            release.set()

    def test_api_explains_model_timeout_and_exposes_recovery(self):
        app = create_app(Settings("127.0.0.1", 8090, Path(self.directory.name) / "api.db"))
        app.state.runtime.writer = ArkReportWriter("https://example.invalid", "demo", "test-key")
        client = TestClient(app)
        with patch.object(ArkReportWriter, "rewrite_with_usage", side_effect=ModelCallTimeout("超时")):
            response = client.post("/api/reports/daily", json={
                "report_date": "2026-10-14", "completed": "完成接口联调",
            })
        self.assertEqual(504, response.status_code)
        self.assertIn("写作进化", response.json()["detail"])
        runs = client.get("/api/agents/recoverable").json()["runs"]
        self.assertEqual("ModelCallTimeout", runs[0]["error_code"])

    def test_interrupted_model_call_reservation_survives_resume(self):
        writer = ArkReportWriter("https://example.invalid", "demo", "test-key")
        usage = {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100, "source": "provider"}
        runtime = MultiAgentRuntime(self.store, writer)
        with patch.object(ArkReportWriter, "rewrite_with_usage", side_effect=[
            RuntimeError("interrupted"), (DRAFT, True, "测试", usage),
        ]):
            with self.assertRaises(RuntimeError):
                runtime.run_report(self.report["id"], SOURCE, DRAFT)
            run = self.store.list_agent_runs(1)[0]
            reserved = run["token_used"]
            self.assertGreater(reserved, 100)
            self.assertEqual(reserved, run["token_estimated"])
            runtime.resume_report(run["id"])
        resumed = self.store.get_agent_run(run["id"])
        self.assertEqual(reserved + 100, resumed["token_used"])
        self.assertEqual(reserved, resumed["token_estimated"])

    def test_token_limit_stops_model_before_call(self):
        writer = ArkReportWriter("https://example.invalid", "demo", "test-key")
        runtime = MultiAgentRuntime(self.store, writer)
        runtime.MAX_TOTAL_TOKENS = 1
        with patch.object(ArkReportWriter, "rewrite_with_usage") as call:
            with self.assertRaises(RunBudgetExceeded):
                runtime.run_report(self.report["id"], SOURCE, DRAFT)
        call.assert_not_called()
        self.assertEqual("budget_exhausted", self.store.list_agent_runs(1)[0]["status"])

    def test_estimated_usage_is_labeled_and_counted(self):
        writer = ArkReportWriter("https://example.invalid", "demo", "test-key")
        usage = {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100,
                 "source": "estimated"}
        with patch.object(ArkReportWriter, "rewrite_with_usage", return_value=(DRAFT, True, "测试", usage)):
            result = MultiAgentRuntime(self.store, writer).run_report(self.report["id"], SOURCE, DRAFT)
        run = self.store.get_agent_run(result["run_id"])
        self.assertEqual(100, run["token_used"])
        self.assertEqual(100, run["token_estimated"])

    def test_provider_overage_is_recorded_and_blocks_report(self):
        writer = ArkReportWriter("https://example.invalid", "demo", "test-key")
        usage = {"input_tokens": 12000, "output_tokens": 1, "total_tokens": 12001,
                 "source": "provider"}
        with patch.object(ArkReportWriter, "rewrite_with_usage", return_value=(DRAFT, True, "测试", usage)):
            with self.assertRaises(RunBudgetExceeded):
                MultiAgentRuntime(self.store, writer).run_report(self.report["id"], SOURCE, DRAFT)
        run = self.store.list_agent_runs(1)[0]
        self.assertEqual("budget_exhausted", run["status"])
        self.assertEqual(12001, run["token_used"])
        self.assertEqual(DRAFT, self.store.get_report(self.report["id"])["draft"])

    def test_failed_writer_resumes_same_run_without_repeating_completed_steps(self):
        writer = Writer(failures=1)
        runtime = MultiAgentRuntime(self.store, writer)
        with self.assertRaises(RuntimeError):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run = self.store.list_agent_runs(1)[0]
        self.assertEqual("facts_ready", self.store.get_agent_checkpoint(run["id"])["stage"])
        handoff = self.store.get_agent_checkpoint(run["id"])["handoff"]
        self.assertEqual(["F0001"], [item["id"] for item in handoff["evidence"]])
        self.assertEqual(["Planner Agent", "Fact Agent"], [step["agent_name"] for step in run["steps"]])
        self.assertEqual([run["id"]], [item["id"] for item in runtime.recoverable_runs()])

        result = runtime.resume_report(run["id"])
        resumed = self.store.get_agent_run(run["id"])
        self.assertEqual(run["id"], result["run_id"])
        self.assertEqual("passed", resumed["status"])
        self.assertEqual(1, resumed["recovery_count"])
        self.assertEqual(2, writer.calls)
        self.assertEqual(1, sum(step["agent_name"] == "Planner Agent" for step in resumed["steps"]))
        self.assertEqual(1, sum(step["agent_name"] == "Fact Agent" for step in resumed["steps"]))
        self.assertEqual(handoff, self.store.get_agent_checkpoint(run["id"])["handoff"])
        self.assertEqual(DRAFT, self.store.get_report(self.report["id"])["draft"])
        self.assertEqual([], runtime.recoverable_runs())

    def test_task_node_failure_resumes_only_pending_nodes(self):
        source = {
            "kind": "daily", "facts": ["完成前端联调", "修复接口异常"],
            "fact_sources": [
                {"source_type": "git_commit", "project_id": "p1", "commit_hash": "a" * 40},
                {"source_type": "git_commit", "project_id": "p2", "commit_hash": "b" * 40},
            ],
            "projects": [{"id": "p1", "name": "前端"}, {"id": "p2", "name": "接口"}],
        }
        draft = "## 今日完成\n- 完成前端联调\n- 修复接口异常\n## 遇到的问题\n## 明日计划\n"
        report = self.store.create_report("daily", "2026-10-15", source, draft, 1)
        calls = 0

        def interrupt_second(role, operation, payload):
            nonlocal calls
            if role == "Fact Agent" and operation == "execute_task":
                calls += 1
                if calls == 2:
                    raise RuntimeError("task unavailable")

        executor = InterceptingRoleExecutor(interrupt_second)
        runtime = MultiAgentRuntime(self.store, Writer(), executor)
        with self.assertRaises(RuntimeError):
            runtime.run_report(report["id"], source, draft)
        run = self.store.list_agent_runs(1)[0]
        checkpoint = self.store.get_agent_checkpoint(run["id"])
        self.assertEqual("planned", checkpoint["stage"])
        self.assertEqual({"T001": "completed", "T002": "pending"}, checkpoint["handoff"]["task_states"])
        runtime.resume_report(run["id"])
        completed = self.store.get_agent_run(run["id"])
        fact_task_ids = [step["input"]["task_id"] for step in completed["steps"]
                         if step["agent_name"] == "Fact Agent"]
        self.assertEqual(["T001", "T002"], fact_task_ids)
        self.assertEqual("verified", runtime.replay_report(run["id"])["status"])
        executor.close()

    def test_changed_role_permissions_in_checkpoint_block_resume(self):
        runtime = MultiAgentRuntime(self.store, Writer(failures=1))
        with self.assertRaises(RuntimeError):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run_id = self.store.list_agent_runs(1)[0]["id"]
        handoff = self.store.get_agent_checkpoint(run_id)["handoff"]
        handoff["permissions"]["Writer Agent"]["operations"] = ["unknown_operation"]
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("UPDATE agent_checkpoints SET handoff_json=? WHERE run_id=?",
                               (json.dumps(handoff, ensure_ascii=False), run_id))
        with self.assertRaisesRegex(ValueError, "任务交接包"):
            runtime.resume_report(run_id)
        self.assertEqual("failed", self.store.get_agent_run(run_id)["status"])

    def test_changed_role_permissions_in_snapshot_diverge_on_replay(self):
        run = MultiAgentRuntime(self.store, Writer()).run_report(self.report["id"], SOURCE, DRAFT)
        run_id = run["run_id"]
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            row = connection.execute(
                "SELECT step_id,handoff_json FROM agent_step_snapshots WHERE run_id=? ORDER BY created_at LIMIT 1",
                (run_id,),
            ).fetchone()
            handoff = json.loads(row[1])
            handoff["permissions"]["Critic Agent"]["reads"] = ["all_data"]
            connection.execute("UPDATE agent_step_snapshots SET handoff_json=? WHERE step_id=?",
                               (json.dumps(handoff, ensure_ascii=False), row[0]))
        replay = MultiAgentRuntime(self.store, Writer()).replay_report(run_id)
        self.assertEqual("diverged", replay["status"])
        self.assertEqual("任务或证据交接不一致", replay["steps"][0]["reason"])

    def test_note_correction_and_removal_do_not_change_existing_report_plan(self):
        first = self.store.add_project("前端", "project-one", "plain")
        second = self.store.add_project("接口", "project-two", "plain")
        note = self.store.add_memory("project", "note", "关注接口兼容性", second["id"])
        source = {**SOURCE, "projects": [first, second],
                  "memory_context": self.store.memory_context([first["id"], second["id"]])}
        report = self.store.create_report("daily", "2026-10-14", source, DRAFT, 1)
        runtime = MultiAgentRuntime(self.store, Writer(failures=1))
        with self.assertRaises(RuntimeError):
            runtime.run_report(report["id"], source, DRAFT)
        run_id = self.store.list_agent_runs(1)[0]["id"]
        original_handoff = self.store.get_agent_checkpoint(run_id)["handoff"]
        self.assertEqual([note["id"]], original_handoff["plan"]["tasks"][0]["context_note_ids"])

        corrected = self.store.correct_memory(note["id"], "接口背景已更新")
        corrected_source = {**SOURCE, "projects": [first, second],
                            "memory_context": self.store.memory_context([first["id"], second["id"]])}
        corrected_report = self.store.create_report("daily", "2026-10-15", corrected_source, DRAFT, 1)
        corrected_run = MultiAgentRuntime(self.store, Writer()).run_report(
            corrected_report["id"], corrected_source, DRAFT,
        )
        corrected_tasks = self.store.get_agent_checkpoint(corrected_run["run_id"])["handoff"]["plan"]["tasks"]
        self.assertEqual([corrected["id"]], corrected_tasks[0]["context_note_ids"])
        self.store.remove_memory(corrected["id"])
        self.assertEqual([], self.store.memory_context([second["id"]])["project_notes"])
        runtime.resume_report(run_id)
        self.assertEqual(original_handoff, self.store.get_agent_checkpoint(run_id)["handoff"])
        self.assertEqual("verified", runtime.replay_report(run_id)["status"])

        next_source = {**SOURCE, "projects": [first, second],
                       "memory_context": self.store.memory_context([first["id"], second["id"]])}
        next_report = self.store.create_report("daily", "2026-10-16", next_source, DRAFT, 1)
        next_run = MultiAgentRuntime(self.store, Writer()).run_report(next_report["id"], next_source, DRAFT)
        next_tasks = self.store.get_agent_checkpoint(next_run["run_id"])["handoff"]["plan"]["tasks"]
        self.assertEqual([first["id"], second["id"]], [task["project_id"] for task in next_tasks[:2]])
        self.assertTrue(all("context_note_ids" not in task for task in next_tasks))

    def test_stale_run_resumes_after_writer_checkpoint(self):
        writer = Writer()
        interrupted = False

        def interrupt_critic(role, operation, payload):
            nonlocal interrupted
            if role == "Critic Agent" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        executor = InterceptingRoleExecutor(interrupt_critic)
        runtime = MultiAgentRuntime(self.store, writer, executor)
        with self.assertRaises(KeyboardInterrupt):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run = self.store.list_agent_runs(1)[0]
        self.assertEqual("running", run["status"])
        self.assertEqual("writer_ready", self.store.get_agent_checkpoint(run["id"])["stage"])
        self.assertEqual([], runtime.recoverable_runs())
        age_run(self.store, run["id"])

        result = runtime.resume_report(run["id"])
        self.assertEqual("passed", result["decision"])
        self.assertGreaterEqual(self.store.get_agent_run(run["id"])["duration_ms"], 45000)
        self.assertEqual(1, writer.calls)
        self.assertEqual(1, sum(step["agent_name"] == "Writer Agent" for step in self.store.get_agent_run(run["id"])["steps"]))
        executor.close()

    def test_replanned_checkpoint_resumes_without_repeating_planner_adjustment(self):
        interrupted = False

        def interrupt_repair(role, operation, payload):
            nonlocal interrupted
            if role == "Writer Agent" and operation == "revise_draft" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        executor = InterceptingRoleExecutor(interrupt_repair)
        runtime = MultiAgentRuntime(self.store, NeedsRepairWriter(), executor)
        with self.assertRaises(KeyboardInterrupt):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run_id = self.store.list_agent_runs(1)[0]["id"]
        checkpoint = self.store.get_agent_checkpoint(run_id)
        self.assertEqual("replanned", checkpoint["stage"])
        self.assertEqual("planned", checkpoint["handoff"]["plan_revisions"][-1]["nodes"][0]["status"])
        planner_steps = sum(step["agent_name"] == "Planner Agent" for step in self.store.get_agent_run(run_id)["steps"])
        age_run(self.store, run_id)

        result = runtime.resume_report(run_id)
        self.assertEqual("passed", result["decision"])
        resumed = self.store.get_agent_run(run_id)
        self.assertEqual(planner_steps, sum(step["agent_name"] == "Planner Agent" for step in resumed["steps"]))
        self.assertTrue(all(node["status"] == "completed"
                            for node in self.store.get_agent_checkpoint(run_id)["handoff"]["plan_revisions"][-1]["nodes"]))
        executor.close()

    def test_gated_checkpoint_recovers_atomic_completion(self):
        runtime = MultiAgentRuntime(self.store, Writer())
        with patch.object(self.store, "complete_daily_run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run = self.store.list_agent_runs(1)[0]
        self.assertEqual("gated", self.store.get_agent_checkpoint(run["id"])["stage"])
        step_count = len(run["steps"])
        age_run(self.store, run["id"])

        runtime.resume_report(run["id"])
        self.assertEqual(step_count, len(self.store.get_agent_run(run["id"])["steps"]))
        self.assertEqual("passed", self.store.get_agent_run(run["id"])["status"])

    def test_user_edited_draft_blocks_resume_and_preserves_text(self):
        runtime = MultiAgentRuntime(self.store, Writer(failures=1))
        with self.assertRaises(RuntimeError):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run_id = self.store.list_agent_runs(1)[0]["id"]
        self.store.update_report_draft(self.report["id"], "用户修改")
        self.assertEqual([], runtime.recoverable_runs())
        with self.assertRaisesRegex(ValueError, "草稿已修改"):
            runtime.resume_report(run_id)
        self.assertEqual("用户修改", self.store.get_report(self.report["id"])["draft"])

    def test_checkpoint_redacts_candidate_and_trace_does_not_expose_it(self):
        sensitive = DRAFT + "\ntoken=example-value\n"
        report = self.store.create_report("daily", "2026-10-14", SOURCE, sensitive, 1)
        runtime = MultiAgentRuntime(self.store, Writer(failures=1))
        with self.assertRaises(RuntimeError):
            runtime.run_report(report["id"], SOURCE, sensitive)
        run = self.store.list_agent_runs(1)[0]
        checkpoint = self.store.get_agent_checkpoint(run["id"])
        self.assertNotIn("example-value", checkpoint["candidate_draft"])
        self.assertNotIn("example-value", str(self.store.list_agent_step_snapshots(run["id"])))
        self.assertNotIn("example-value", str(run["steps"]))

    def test_recovery_limit_and_stale_owner_fencing(self):
        runtime = MultiAgentRuntime(self.store, Writer(failures=2))
        with self.assertRaises(RuntimeError):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run_id = self.store.list_agent_runs(1)[0]["id"]
        self.assertTrue(self.store.claim_agent_run(run_id))
        with self.assertRaisesRegex(ValueError, "接管"):
            self.store.add_agent_step(run_id, "Old Agent", "passed", {}, {}, {}, 0, expected_recovery_count=0)
        self.store.finish_agent_run(run_id, "failed", 0, expected_recovery_count=0)
        self.assertEqual("running", self.store.get_agent_run(run_id)["status"])
        self.store.finish_agent_run(run_id, "failed", 0, expected_recovery_count=1)
        with self.assertRaises(RuntimeError):
            runtime.resume_report(run_id)
        self.assertEqual([], runtime.recoverable_runs())
        with self.assertRaises(ValueError):
            runtime.resume_report(run_id)
        self.assertEqual(2, self.store.get_agent_run(run_id)["recovery_count"])

    def test_api_lists_and_resumes_failed_run(self):
        app = create_app(Settings("127.0.0.1", 8090, Path(self.directory.name) / "api.db"))
        client = TestClient(app)
        report = app.state.store.create_report("daily", "2026-10-14", SOURCE, DRAFT, 1)
        app.state.runtime.writer = Writer(failures=1)
        with self.assertRaises(RuntimeError):
            app.state.runtime.run_report(report["id"], SOURCE, DRAFT)
        recoverable = client.get("/api/agents/recoverable")
        self.assertEqual(200, recoverable.status_code)
        self.assertEqual(1, len(recoverable.json()["runs"]))
        run_id = recoverable.json()["runs"][0]["id"]
        response = client.post(f"/api/agents/{run_id}/resume")
        self.assertEqual(200, response.status_code)
        self.assertEqual(run_id, response.json()["agent_trace"]["id"])
        self.assertEqual("passed", response.json()["agent_trace"]["status"])
        self.assertEqual([], client.get("/api/agents/recoverable").json()["runs"])

    def test_existing_checkpoint_table_gets_handoff_column(self):
        path = Path(self.directory.name) / "legacy.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("""CREATE TABLE agent_checkpoints (
                run_id TEXT PRIMARY KEY,stage TEXT NOT NULL,attempt INTEGER NOT NULL,
                candidate_draft TEXT NOT NULL,review_json TEXT NOT NULL,input_hash TEXT NOT NULL,
                llm_used INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL
            )""")
        Store(path)
        with closing(sqlite3.connect(path)) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(agent_checkpoints)")]
        self.assertIn("handoff_json", columns)

    def test_legacy_empty_handoff_rebuilds_from_unchanged_source(self):
        runtime = MultiAgentRuntime(self.store, Writer(failures=1))
        with self.assertRaises(RuntimeError):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run_id = self.store.list_agent_runs(1)[0]["id"]
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("UPDATE agent_checkpoints SET handoff_json='{}' WHERE run_id=?", (run_id,))
        result = runtime.resume_report(run_id)
        self.assertEqual("passed", result["decision"])
        self.assertEqual(["F0001"], [item["id"] for item in self.store.get_agent_checkpoint(run_id)["handoff"]["evidence"]])

    def test_total_budget_blocks_further_recovery(self):
        writer = Writer(failures=1)
        runtime = MultiAgentRuntime(self.store, writer)
        with self.assertRaises(RuntimeError):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run_id = self.store.list_agent_runs(1)[0]["id"]
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("UPDATE agent_runs SET duration_ms=? WHERE id=?", (90000, run_id))
        with self.assertRaisesRegex(ValueError, "预算已耗尽"):
            runtime.resume_report(run_id)
        self.assertEqual(1, writer.calls)
        self.assertEqual("budget_exhausted", self.store.get_agent_run(run_id)["status"])
        self.assertEqual([], runtime.recoverable_runs())

    def test_initial_budget_exhaustion_is_recorded(self):
        runtime = MultiAgentRuntime(self.store, Writer())
        runtime.MAX_TOTAL_SECONDS = 0
        with self.assertRaises(RunBudgetExceeded):
            runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run = self.store.list_agent_runs(1)[0]
        self.assertEqual("budget_exhausted", run["status"])
        self.assertEqual("RunBudgetExceeded", run["error_code"])

    def test_replay_verifies_all_steps_without_rerunning_writer(self):
        writer = Writer()
        runtime = MultiAgentRuntime(self.store, writer)
        result = runtime.run_report(self.report["id"], SOURCE, DRAFT)
        replay = runtime.replay_report(result["run_id"])
        self.assertEqual("verified", replay["status"])
        self.assertEqual(5, len(replay["steps"]))
        self.assertEqual(1, writer.calls)
        self.assertNotIn("完成接口联调", str(replay))

    def test_replay_identifies_changed_writer_snapshot_and_source(self):
        runtime = MultiAgentRuntime(self.store, Writer())
        result = runtime.run_report(self.report["id"], SOURCE, DRAFT)
        run_id = result["run_id"]
        writer_step = next(item for item in self.store.get_agent_run(run_id)["steps"] if item["agent_name"] == "Writer Agent")
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("UPDATE agent_step_snapshots SET candidate_draft='篡改' WHERE step_id=?", (writer_step["id"],))
        replay = runtime.replay_report(run_id)
        self.assertEqual("diverged", replay["status"])
        self.assertIn("Writer 候选快照", next(item for item in replay["steps"] if item["step_id"] == writer_step["id"])["reason"])
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("UPDATE reports SET source_json=? WHERE id=?", ('{"kind":"daily","facts":[]}', self.report["id"]))
        self.assertEqual("来源快照已变化", runtime.replay_report(run_id)["steps"][0]["reason"])

    def test_old_run_without_step_snapshots_is_unavailable(self):
        run_id = self.store.start_agent_run("daily_report", self.report["id"])
        self.store.add_agent_step(run_id, "Planner Agent", "passed", {}, {}, {}, 0)
        replay = MultiAgentRuntime(self.store, Writer()).replay_report(run_id)
        self.assertEqual("unavailable", replay["status"])

    def test_old_replay_rule_version_is_unavailable(self):
        runtime = MultiAgentRuntime(self.store, Writer())
        run_id = runtime.run_report(self.report["id"], SOURCE, DRAFT)["run_id"]
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("UPDATE agent_step_snapshots SET replay_version=2 WHERE run_id=?", (run_id,))
        replay = runtime.replay_report(run_id)
        self.assertEqual("unavailable", replay["status"])
        self.assertIn("版本不兼容", replay["reason"])

    def test_replay_identifies_changed_gate_decision_and_damaged_snapshot(self):
        runtime = MultiAgentRuntime(self.store, Writer())
        run_id = runtime.run_report(self.report["id"], SOURCE, DRAFT)["run_id"]
        gate_step = next(item for item in self.store.get_agent_run(run_id)["steps"] if item["agent_name"] == "Gate Agent")
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("UPDATE agent_steps SET status='needs_review' WHERE id=?", (gate_step["id"],))
        replay = runtime.replay_report(run_id)
        self.assertEqual("diverged", replay["status"])
        self.assertEqual("Gate 决策不一致", replay["steps"][-1]["reason"])
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("UPDATE agent_step_snapshots SET review_json='invalid' WHERE step_id=?", (gate_step["id"],))
        self.assertEqual("unavailable", runtime.replay_report(run_id)["status"])

    def test_replay_api_returns_summary_without_report_body(self):
        app = create_app(Settings("127.0.0.1", 8090, Path(self.directory.name) / "replay-api.db"))
        client = TestClient(app)
        report = app.state.store.create_report("daily", "2026-10-14", SOURCE, DRAFT, 1)
        run_id = app.state.runtime.run_report(report["id"], SOURCE, DRAFT)["run_id"]
        response = client.get(f"/api/agents/{run_id}/replay")
        self.assertEqual(200, response.status_code)
        self.assertEqual("verified", response.json()["status"])
        self.assertNotIn("完成接口联调", response.text)


if __name__ == "__main__":
    unittest.main()
