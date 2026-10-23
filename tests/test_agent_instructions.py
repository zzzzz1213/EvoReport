import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from evoreport.agent_instructions import instruction_hash
from evoreport.agent_runtime import MultiAgentRuntime
from evoreport.api import create_app
from evoreport.config import Settings
from evoreport.evolution import EvolutionService
from evoreport.llm import ArkReportWriter
from evoreport.store import Store


DRAFT = "# 工作日报\n\n## 今日完成\n- 完成接口联调\n\n## 遇到的问题\n- 暂无\n\n## 明日计划\n- 补充测试\n"
SOURCE = {"kind": "daily", "facts": ["完成接口联调"], "projects": []}


def add_case(store, path, split, source=SOURCE, draft=DRAFT):
    report = store.create_report("daily", "2026-10-16", source, draft, 1)
    store.confirm_report(report["id"], draft)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE evaluation_cases SET split=? WHERE report_id=?", (split, report["id"]))
    return report


def add_prompt_monitor_case(store, path, version, created_at, draft=DRAFT, final_text=DRAFT,
                            split="validation", llm_used=True):
    report = store.create_report("daily", "2026-10-16", SOURCE, draft, 1)
    run_id = store.start_agent_run("daily_report", report["id"], {
        "writer_prompt": {"version": version, "sha256": store.get_writer_prompt(version)["sha256"]},
    })
    store.add_agent_step(
        run_id, "Writer Agent", "passed", {"attempt": 0}, {"llm_used": llm_used}, {}, 1,
    )
    store.finish_agent_run(run_id, "passed", 1)
    store.confirm_report(report["id"], final_text)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE agent_runs SET created_at=? WHERE id=?", (created_at, run_id))
        connection.execute("UPDATE evaluation_cases SET split=? WHERE report_id=?", (split, report["id"]))
    return store.get_report(report["id"])


class PromptWriter:
    enabled = True

    def __init__(self, mode="improve"):
        self.mode = mode
        self.calls = []

    def rewrite(self, draft, instruction=None):
        self.calls.append((draft, instruction))
        if self.mode == "unavailable":
            return draft, False, "model unavailable"
        if self.mode == "same":
            return draft, True, "model output"
        if self.mode == "holdout_regression" and "Holdout 完成接口联调" in draft and "表达简洁" in instruction:
            return draft.replace("Holdout 完成接口联调", "暂无已记录内容"), True, "model output"
        return (draft if "表达简洁" in instruction else draft.replace("- ", "1. ")), True, "model output"


class AgentInstructionTests(unittest.TestCase):
    def test_writer_prompt_monitor_uses_real_validation_runs_and_auto_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt-monitor.db"
            store = Store(path)
            candidate = store.create_writer_prompt("表达简洁。")
            for index in range(5):
                add_prompt_monitor_case(store, path, 1, f"2026-10-01T00:00:0{index}+00:00")
            switch_id = str(uuid.uuid4())
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE writer_prompt_versions SET status='inactive' WHERE version=1")
                connection.execute("UPDATE writer_prompt_versions SET status='active' WHERE version=?",
                                   (candidate["version"],))
                connection.execute(
                    """INSERT INTO writer_prompt_switches(
                    id,previous_version,active_version,source,created_at) VALUES(?,?,?,?,?)""",
                    (switch_id, 1, candidate["version"], "evaluation", "2026-10-02T00:00:00+00:00"),
                )
            weak_draft = DRAFT.replace("完成接口联调", "暂无已记录内容")
            reports = [add_prompt_monitor_case(
                store, path, candidate["version"], f"2026-10-03T00:00:{index:02d}+00:00",
                weak_draft, DRAFT,
            ) for index in range(10)]
            add_prompt_monitor_case(
                store, path, candidate["version"], "2026-10-03T00:01:00+00:00",
                weak_draft, DRAFT, split="holdout",
            )
            add_prompt_monitor_case(
                store, path, candidate["version"], "2026-10-03T00:01:01+00:00",
                weak_draft, DRAFT, llm_used=False,
            )
            service = EvolutionService(store)
            monitor = service.writer_prompt_monitor()
            self.assertEqual(10, monitor["active_sample_count"])
            self.assertEqual(5, monitor["baseline_sample_count"])
            self.assertLess(monitor["active_metrics"]["score"], monitor["baseline_metrics"]["score"])
            decision = service.maybe_auto_rollback_writer_prompt(reports[-1]["id"])
            self.assertEqual("rolled_back", decision["decision"])
            self.assertEqual(1, store.active_writer_prompt()["version"])
            self.assertEqual("auto_rollback", store.latest_writer_prompt_switch()["source"])
            self.assertEqual(decision, store.latest_writer_prompt_monitor_check())
            self.assertIsNone(service.maybe_auto_rollback_writer_prompt(reports[-1]["id"]))

    def test_writer_prompt_monitor_waits_for_two_windows_and_skips_manual_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompt-monitor-gate.db"
            store = Store(path)
            candidate = store.create_writer_prompt("表达简洁。")
            for index in range(5):
                add_prompt_monitor_case(store, path, 1, f"2026-10-01T00:00:0{index}+00:00")
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE writer_prompt_versions SET status='inactive' WHERE version=1")
                connection.execute("UPDATE writer_prompt_versions SET status='active' WHERE version=?",
                                   (candidate["version"],))
                connection.execute(
                    """INSERT INTO writer_prompt_switches(
                    id,previous_version,active_version,source,created_at) VALUES(?,?,?,?,?)""",
                    (str(uuid.uuid4()), 1, candidate["version"], "evaluation", "2026-10-02T00:00:00+00:00"),
                )
            reports = [add_prompt_monitor_case(
                store, path, candidate["version"], f"2026-10-03T00:00:0{index}+00:00",
                DRAFT.replace("完成接口联调", "暂无已记录内容"), DRAFT,
            ) for index in range(9)]
            service = EvolutionService(store)
            self.assertIsNone(service.maybe_auto_rollback_writer_prompt(reports[-1]["id"]))
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE writer_prompt_switches SET source='manual'")
            last = add_prompt_monitor_case(
                store, path, candidate["version"], "2026-10-03T00:00:10+00:00",
                DRAFT.replace("完成接口联调", "暂无已记录内容"), DRAFT,
            )
            self.assertIsNone(service.maybe_auto_rollback_writer_prompt(last["id"]))
            self.assertEqual(candidate["version"], store.active_writer_prompt()["version"])

    def test_writer_prompt_versions_are_immutable_and_can_be_switched_back(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "prompts.db")
            baseline = store.active_writer_prompt()
            self.assertEqual(1, baseline["version"])
            self.assertEqual(instruction_hash(baseline["instruction"]), baseline["sha256"])
            candidate = store.create_writer_prompt("表达简洁，突出已完成的结果。")
            self.assertEqual("manual", candidate["source"])
            self.assertEqual("inactive", candidate["status"])
            self.assertNotEqual(baseline["sha256"], candidate["sha256"])
            with self.assertRaisesRegex(ValueError, "已存在"):
                store.create_writer_prompt("表达简洁，突出已完成的结果。")
            with self.assertRaisesRegex(ValueError, "敏感信息"):
                store.create_writer_prompt("token=example")
            with self.assertRaisesRegex(ValueError, "尚未通过"):
                store.activate_writer_prompt(candidate["version"])
            self.assertEqual(1, store.active_writer_prompt()["version"])
            self.assertEqual(0, len(store.list_writer_prompt_switches()))
            self.assertEqual(baseline["instruction"], store.get_writer_prompt(1)["instruction"])

    def test_daily_run_binds_prompt_and_resume_ignores_later_manual_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.db"
            store = Store(path)
            candidate = store.create_writer_prompt("表达简洁，突出已完成的结果。")
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            service = EvolutionService(store, MultiAgentRuntime(store, PromptWriter()))
            service.create_benchmark()
            self.assertEqual("passed", service.evaluate_writer_prompt(candidate["version"])["decision"])
            self.assertTrue(store.activate_writer_prompt(candidate["version"]))
            report = store.create_report("daily", "2026-10-16", SOURCE, DRAFT, 1)
            runtime = MultiAgentRuntime(store, ArkReportWriter("", "", ""))
            with patch.object(ArkReportWriter, "rewrite", side_effect=RuntimeError("temporary")):
                with self.assertRaises(RuntimeError):
                    runtime.run_report(report["id"], SOURCE, DRAFT)
            run = store.list_agent_runs(1)[0]
            self.assertEqual(candidate["version"], run["instruction_snapshot"]["writer_prompt"]["version"])
            self.assertEqual(candidate["sha256"], run["instruction_snapshot"]["writer_prompt"]["sha256"])
            store.activate_writer_prompt(1)
            seen = []

            def rewrite(draft, instruction=None):
                seen.append(instruction)
                return draft, False, "本地测试"

            with patch.object(ArkReportWriter, "rewrite", side_effect=rewrite):
                result = runtime.resume_report(run["id"])
            self.assertEqual("passed", result["decision"])
            self.assertEqual([store.get_writer_prompt(candidate["version"])["instruction"]], seen)
            self.assertEqual(candidate["version"], store.get_agent_run(run["id"])["instruction_snapshot"]["writer_prompt"]["version"])
            self.assertEqual("verified", runtime.replay_report(run["id"])["status"])
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE writer_prompt_versions SET instruction='changed' WHERE version=?",
                                   (candidate["version"],))
            self.assertEqual("unavailable", runtime.replay_report(run["id"])["status"])

    def test_api_creates_manual_version_and_runs_record_rule_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.db"
            client = TestClient(create_app(Settings("127.0.0.1", 8090, path)))
            created = client.post("/api/agents/writer-prompts", json={"addendum": "语气客观。"})
            self.assertEqual(201, created.status_code)
            version = created.json()["version"]
            self.assertEqual("inactive", created.json()["status"])
            self.assertEqual(409, client.post(f"/api/agents/writer-prompts/{version}/activate").status_code)
            versions = client.get("/api/agents/writer-prompts").json()["versions"]
            self.assertEqual(1, next(item for item in versions if item["status"] == "active")["version"])
            self.assertNotIn("instruction", versions[0])
            store = client.app.state.store
            report = store.create_report("daily", "2026-10-16", SOURCE, DRAFT, 1)
            client.app.state.runtime.run_report(report["id"], SOURCE, DRAFT)
            run = store.list_agent_runs(1)[0]
            self.assertEqual(1, run["instruction_snapshot"]["writer_prompt"]["version"])
            self.assertEqual(1, run["instruction_snapshot"]["critic_rules"]["version"])
            review = client.app.state.runtime.review_final(report["id"], SOURCE, DRAFT)
            self.assertEqual(1, store.get_agent_run(review["run_id"])["instruction_snapshot"]["critic_rules"]["version"])
            self.assertEqual(200, client.post("/api/agents/writer-prompts/1/activate").status_code)
            self.assertEqual(404, client.post("/api/agents/writer-prompts/999/activate").status_code)
            self.assertEqual(409, client.post(f"/api/agents/writer-prompts/{version}/evaluate").status_code)

    def test_prompt_evaluation_gate_and_single_use_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluate.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            candidate = store.create_writer_prompt("表达简洁，突出已完成的结果。")
            runtime = MultiAgentRuntime(store, PromptWriter())
            service = EvolutionService(store, runtime)
            service.create_benchmark()
            result = service.evaluate_writer_prompt(candidate["version"])
            self.assertEqual("passed", result["decision"])
            self.assertEqual(8, len(runtime.writer.calls))
            self.assertEqual(3, len(result["evidence"]["validation_cases"]))
            self.assertEqual(1, len(result["evidence"]["holdout_cases"]))
            self.assertTrue(all(runtime.writer.calls[index][0] == runtime.writer.calls[index + 1][0]
                                for index in range(0, 8, 2)))
            self.assertNotIn(DRAFT, str(result))
            self.assertTrue(store.activate_writer_prompt(candidate["version"]))
            self.assertEqual("evaluation", store.latest_writer_prompt_switch()["source"])
            self.assertTrue(store.activate_writer_prompt(1))
            self.assertEqual("manual", store.latest_writer_prompt_switch()["source"])
            self.assertTrue(store.activate_writer_prompt(candidate["version"]))
            self.assertEqual(3, len(store.list_writer_prompt_switches()))
            next_candidate = store.create_writer_prompt("使用客观语气。")
            with self.assertRaisesRegex(ValueError, "Holdout"):
                service.evaluate_writer_prompt(next_candidate["version"])
            self.assertEqual(8, len(runtime.writer.calls))

    def test_validation_failure_and_model_failure_do_not_read_holdout(self):
        for mode, expected in (("same", "failed"), ("unavailable", "error")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "failure.db"
                store = Store(path)
                for _ in range(3):
                    add_case(store, path, "validation")
                add_case(store, path, "holdout")
                candidate = store.create_writer_prompt("表达简洁。")
                writer = PromptWriter(mode)
                service = EvolutionService(store, MultiAgentRuntime(store, writer))
                service.create_benchmark()
                original = store.benchmark_prompt_cases

                def validation_only(benchmark, split, limit):
                    self.assertEqual("validation", split)
                    return original(benchmark, split, limit)

                with patch.object(store, "benchmark_prompt_cases", side_effect=validation_only):
                    result = service.evaluate_writer_prompt(candidate["version"])
                self.assertEqual(expected, result["decision"])
                self.assertIsNone(result["metrics"]["holdout"])
                self.assertIsNone(store.list_evaluation_benchmarks()[0]["holdout_used_at"])
                with self.assertRaisesRegex(ValueError, "不能重复"):
                    service.evaluate_writer_prompt(candidate["version"])
                with self.assertRaisesRegex(ValueError, "尚未通过"):
                    store.activate_writer_prompt(candidate["version"])

    def test_changed_initial_draft_is_rejected_before_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft-fingerprint.db"
            store = Store(path)
            validation = [add_case(store, path, "validation") for _ in range(3)]
            add_case(store, path, "holdout")
            candidate = store.create_writer_prompt("表达简洁。")
            writer = PromptWriter()
            service = EvolutionService(store, MultiAgentRuntime(store, writer))
            benchmark = service.create_benchmark()
            self.assertEqual(64, len(benchmark["validation_draft_hash"]))
            self.assertEqual(64, len(benchmark["holdout_draft_hash"]))
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE reports SET initial_draft=? WHERE id=?",
                                   (DRAFT + "\n额外内容", validation[0]["id"]))
            with self.assertRaisesRegex(ValueError, "初稿快照"):
                service.evaluate_writer_prompt(candidate["version"])
            self.assertEqual([], writer.calls)
            self.assertEqual([], store.list_writer_prompt_evaluations())
            self.assertIsNone(store.list_evaluation_benchmarks()[0]["holdout_used_at"])

    def test_changed_holdout_draft_does_not_consume_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdout-draft.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            holdout = add_case(store, path, "holdout")
            candidate = store.create_writer_prompt("表达简洁。")
            writer = PromptWriter()
            service = EvolutionService(store, MultiAgentRuntime(store, writer))
            service.create_benchmark()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE reports SET initial_draft=? WHERE id=?",
                                   (DRAFT + "\n额外内容", holdout["id"]))
            result = service.evaluate_writer_prompt(candidate["version"])
            self.assertEqual("error", result["decision"])
            self.assertIn("初稿快照", result["reason"])
            self.assertEqual(6, len(writer.calls))
            self.assertEqual([], result["evidence"]["holdout_cases"])
            self.assertIsNone(store.list_evaluation_benchmarks()[0]["holdout_used_at"])

    def test_legacy_benchmark_cannot_claim_current_draft_as_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-benchmark.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            benchmark = EvolutionService(store).create_benchmark()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "UPDATE evaluation_benchmarks SET validation_draft_hash=NULL,holdout_draft_hash=NULL WHERE version=?",
                    (benchmark["version"],),
                )
            reopened = Store(path)
            legacy = reopened.list_evaluation_benchmarks()[0]
            self.assertIsNone(legacy["validation_draft_hash"])
            with self.assertRaisesRegex(ValueError, "旧基准未冻结初稿指纹"):
                reopened.benchmark_prompt_cases(legacy, "validation", 3)

    def test_existing_benchmark_table_gets_draft_hash_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-schema.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("""CREATE TABLE evaluation_benchmarks (
                    version INTEGER PRIMARY KEY, validation_ids_json TEXT NOT NULL,
                    holdout_ids_json TEXT NOT NULL, validation_hash TEXT NOT NULL,
                    holdout_hash TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    holdout_used_at TEXT, created_at TEXT NOT NULL
                )""")
            Store(path)
            with closing(sqlite3.connect(path)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_benchmarks)")}
            self.assertIn("validation_draft_hash", columns)
            self.assertIn("holdout_draft_hash", columns)

    def test_holdout_regression_blocks_activation_and_new_benchmark_invalidates_old_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdout-gate.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            holdout_source = {**SOURCE, "facts": ["Holdout 完成接口联调"]}
            holdout_draft = DRAFT.replace("完成接口联调", "Holdout 完成接口联调")
            add_case(store, path, "holdout", holdout_source, holdout_draft)
            candidate = store.create_writer_prompt("表达简洁。")
            service = EvolutionService(store, MultiAgentRuntime(store, PromptWriter("holdout_regression")))
            service.create_benchmark()
            failed = service.evaluate_writer_prompt(candidate["version"])
            self.assertEqual("failed", failed["decision"])
            self.assertIsNotNone(failed["metrics"]["holdout"])
            with self.assertRaisesRegex(ValueError, "尚未通过"):
                store.activate_writer_prompt(candidate["version"])

            add_case(store, path, "validation")
            add_case(store, path, "holdout")
            service.create_benchmark()
            service.runtime.writer = PromptWriter()
            passed = service.evaluate_writer_prompt(candidate["version"])
            self.assertEqual("passed", passed["decision"])
            add_case(store, path, "validation")
            add_case(store, path, "holdout")
            service.create_benchmark()
            with self.assertRaisesRegex(ValueError, "尚未通过"):
                store.activate_writer_prompt(candidate["version"])
            self.assertEqual("passed", service.evaluate_writer_prompt(candidate["version"])["decision"])
            self.assertTrue(store.activate_writer_prompt(candidate["version"]))

    def test_prompt_content_change_during_evaluation_marks_result_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            candidate = store.create_writer_prompt("表达简洁。")
            writer = PromptWriter()
            original = writer.rewrite

            def mutate_once(draft, instruction=None):
                if not writer.calls:
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute(
                            "UPDATE writer_prompt_versions SET instruction='changed' WHERE version=?",
                            (candidate["version"],),
                        )
                return original(draft, instruction)

            writer.rewrite = mutate_once
            service = EvolutionService(store, MultiAgentRuntime(store, writer))
            service.create_benchmark()
            result = service.evaluate_writer_prompt(candidate["version"])
            self.assertEqual("stale", result["decision"])
            with self.assertRaisesRegex(ValueError, "指纹"):
                store.activate_writer_prompt(candidate["version"])

    def test_prompt_evaluation_api_allows_activation_only_after_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-evaluation.db"
            client = TestClient(create_app(Settings("127.0.0.1", 8090, path)))
            store = client.app.state.store
            candidate = client.post("/api/agents/writer-prompts", json={"addendum": "表达简洁。"}).json()
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            self.assertEqual(201, client.post("/api/evolution/benchmarks").status_code)
            self.assertEqual(409, client.post(f"/api/agents/writer-prompts/{candidate['version']}/activate").status_code)
            client.app.state.runtime.writer = PromptWriter()
            evaluated = client.post(f"/api/agents/writer-prompts/{candidate['version']}/evaluate")
            self.assertEqual(200, evaluated.status_code)
            self.assertEqual("passed", evaluated.json()["decision"])
            self.assertEqual(200, client.post(f"/api/agents/writer-prompts/{candidate['version']}/activate").status_code)
            self.assertEqual("passed", client.get("/api/agents/writer-prompts").json()["evaluations"][0]["decision"])

    def test_existing_agent_run_table_gets_instruction_snapshot_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """CREATE TABLE agent_runs(id TEXT PRIMARY KEY,workflow TEXT NOT NULL,report_id TEXT,
                    status TEXT NOT NULL,duration_ms INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
                    updated_at TEXT,completed_at TEXT,recovery_count INTEGER NOT NULL DEFAULT 0,error_code TEXT)"""
                )
            store = Store(path)
            run_id = store.start_agent_run("strategy_evaluation")
            self.assertEqual({}, store.get_agent_run(run_id)["instruction_snapshot"])
            self.assertEqual(1, store.active_writer_prompt()["version"])

    def test_existing_writer_prompt_switch_table_gets_monitor_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-switch.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("""CREATE TABLE writer_prompt_switches (
                    id TEXT PRIMARY KEY,previous_version INTEGER NOT NULL,
                    active_version INTEGER NOT NULL,created_at TEXT NOT NULL
                )""")
            Store(path)
            with closing(sqlite3.connect(path)) as connection:
                columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(writer_prompt_switches)"
                )}
                monitor_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='writer_prompt_monitor_checks'"
                ).fetchone()
            self.assertIn("source", columns)
            self.assertIsNotNone(monitor_table)


if __name__ == "__main__":
    unittest.main()
