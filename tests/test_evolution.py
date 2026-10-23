import sqlite3
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from evoreport.api import create_app
from evoreport.config import Settings
from evoreport.evolution import EvolutionService
from evoreport.agent_runtime import MultiAgentRuntime
from evoreport.llm import ArkReportWriter
from evoreport.store import Store


SOURCE = {"kind": "daily", "date": "2026-10-14", "facts": ["完成接口联调"], "projects": [],
          "manual_completed": "完成接口联调", "manual_problems": "", "manual_tomorrow": "补充测试"}
NUMBERED = "# 工作日报\n\n## 今日完成\n1. 完成接口联调\n\n## 遇到的问题\n1. 暂无\n\n## 明日计划\n1. 补充测试\n"
BULLETED = NUMBERED.replace("1. ", "- ")


def add_case(store, path, split, final_text=BULLETED, strategy_version=1, source=SOURCE):
    report = store.create_report("daily", "2026-10-14", source, NUMBERED, strategy_version)
    store.confirm_report(report["id"], final_text)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE evaluation_cases SET split=? WHERE report_id=?", (split, report["id"]))
        connection.execute("UPDATE writing_feedback SET split=? WHERE report_id=?", (split, report["id"]))
    return report


class EvolutionIsolationTests(unittest.TestCase):
    def test_strategy_gate_replay_uses_saved_records_without_reading_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            runtime = MultiAgentRuntime(store, ArkReportWriter("", "", ""))
            service = EvolutionService(store, runtime)
            first = service.propose()
            self.assertEqual("activated", first["decision"])
            with patch.object(store, "benchmark_cases", side_effect=AssertionError("不能重新读取样本")), \
                 patch.object(store, "list_evaluation_cases", side_effect=AssertionError("不能读取 Holdout")):
                replay = runtime.replay_report(first["trace_run_id"])
            self.assertEqual("verified", replay["status"])
            self.assertEqual(4, len(replay["steps"]))
            self.assertNotIn("完成接口联调", str(replay))
            second = service.propose()
            self.assertEqual("deferred", second["decision"])
            self.assertEqual("verified", runtime.replay_report(second["trace_run_id"])["status"])

    def test_strategy_gate_replay_detects_changed_metrics_and_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay-tamper.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            runtime = MultiAgentRuntime(store, ArkReportWriter("", "", ""))
            result = EvolutionService(store, runtime).propose()
            run_id = result["trace_run_id"]
            gate_id = store.get_agent_run(run_id)["steps"][-1]["id"]
            changed_metrics = json.loads(json.dumps(result["metrics"]))
            changed_metrics["candidate_validation"]["score"] = 0.0
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE evaluation_runs SET metrics_json=? WHERE id=?",
                                   (json.dumps(changed_metrics), result["evaluation_run_id"]))
            replay = runtime.replay_report(run_id)
            self.assertEqual("diverged", replay["status"])
            self.assertEqual("评测指标不一致", replay["steps"][2]["reason"])
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE evaluation_runs SET metrics_json=? WHERE id=?",
                                   (json.dumps(result["metrics"]), result["evaluation_run_id"]))
                connection.execute("UPDATE agent_steps SET status='deferred' WHERE id=?", (gate_id,))
            self.assertEqual("门禁决策不一致", runtime.replay_report(run_id)["steps"][-1]["reason"])
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE agent_steps SET status=? WHERE id=?", (result["decision"], gate_id))
                connection.execute("UPDATE evaluation_benchmarks SET fingerprint='changed' WHERE version=?",
                                   (result["evidence"]["benchmark_version"],))
            self.assertEqual("基准或样本数量不一致", runtime.replay_report(run_id)["steps"][0]["reason"])

    def test_old_strategy_trace_has_no_replay_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "old-replay.db")
            run_id = store.start_agent_run("strategy_evaluation")
            replay = MultiAgentRuntime(store, ArkReportWriter("", "", "")).replay_report(run_id)
            self.assertEqual("unavailable", replay["status"])
            self.assertIn("旧运行", replay["reason"])

    def test_validation_coverage_is_frozen_without_holdout_details(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.db"
            store = Store(path)
            manual = {**SOURCE, "projects": [], "fact_sources": [{"source_type": "manual"}]}
            git = {**SOURCE, "projects": [{"id": "a"}],
                   "fact_sources": [{"source_type": "git_commit"}]}
            mixed = {**SOURCE, "projects": [{"id": "a"}, {"id": "b"}],
                     "fact_sources": [{"source_type": "manual"}, {"source_type": "git_commit"}]}
            first_report = add_case(store, path, "validation", NUMBERED.strip(), source=manual)
            add_case(store, path, "validation", BULLETED + "验证" * 130, source=git)
            add_case(store, path, "validation", BULLETED + "验证" * 330, source=mixed)
            holdout = {**SOURCE, "manual_completed": "保留集专用内容",
                       "fact_sources": [{"source_type": "manual"}], "projects": [{"id": "private"}]}
            add_case(store, path, "holdout", "保留集专用内容", source=holdout)
            service = EvolutionService(store)
            benchmark = service.create_benchmark()
            coverage = benchmark["coverage"]
            self.assertEqual(3, coverage["sample_count"])
            self.assertEqual({"manual": 1, "git": 1, "mixed": 1, "unattributed": 0},
                             coverage["source_types"])
            self.assertEqual({"zero": 1, "one": 1, "multiple": 1}, coverage["project_count"])
            self.assertEqual({"short": 1, "medium": 1, "long": 1}, coverage["length"])
            self.assertEqual({"changed": 2, "unchanged": 1, "unknown": 0}, coverage["edits"])
            self.assertEqual([], coverage["warnings"])
            with self.assertRaisesRegex(ValueError, "Validation"):
                store.validation_coverage(store.list_evaluation_cases("holdout"))
            with patch.object(store, "list_evaluation_cases", wraps=store.list_evaluation_cases) as case_reads:
                status = service.status()
            self.assertTrue(all(call.args[0] != "holdout" for call in case_reads.call_args_list))
            self.assertEqual(coverage, status["benchmark"]["coverage"])
            self.assertNotIn("保留集专用内容", str(status))
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE reports SET initial_draft='changed' WHERE id=?", (first_report["id"],))
            self.assertEqual(coverage, service.status()["benchmark"]["coverage"])

    def test_old_benchmark_schema_adds_nullable_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-coverage.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("""CREATE TABLE evaluation_benchmarks (
                    version INTEGER PRIMARY KEY, validation_ids_json TEXT NOT NULL,
                    holdout_ids_json TEXT NOT NULL, validation_hash TEXT NOT NULL,
                    holdout_hash TEXT NOT NULL, validation_draft_hash TEXT,
                    holdout_draft_hash TEXT, fingerprint TEXT NOT NULL,
                    holdout_used_at TEXT, created_at TEXT NOT NULL
                )""")
            Store(path)
            with closing(sqlite3.connect(path)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_benchmarks)")}
            self.assertIn("coverage_json", columns)
            self.assertIn("holdout_run_id", columns)

    def test_auto_rollback_uses_two_fresh_validation_windows_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollback.db"
            store = Store(path)
            previous = store.active_strategy()
            candidate = store.save_strategy({**previous["config"], "numbered": False,
                                             "max_item_length": 40}, {}, "activated")
            store.activate_strategy(candidate["version"], source="evaluation")
            service = EvolutionService(store)
            long_fact = "完成接口联调并补齐异常处理与权限校验" * 3
            source = {**SOURCE, "facts": [long_fact], "manual_completed": long_fact}
            final_text = NUMBERED.replace("完成接口联调", long_fact)
            reports = []
            for _ in range(9):
                reports.append(add_case(store, path, "validation", final_text, candidate["version"], source))
            self.assertIsNone(service.maybe_auto_rollback(reports[-1]["id"]))
            reports.append(add_case(store, path, "validation", final_text, candidate["version"], source))
            with patch.object(store, "list_evaluation_cases", side_effect=AssertionError("不应读取 Holdout")):
                decision = service.maybe_auto_rollback(reports[-1]["id"])
            self.assertEqual("rolled_back", decision["decision"])
            self.assertEqual(previous["version"], store.active_strategy()["version"])
            self.assertEqual("auto_rollback", store.latest_strategy_activation()["source"])
            self.assertEqual([item["id"] for item in reports[:5]], decision["evidence"]["windows"][0]["report_ids"])
            self.assertEqual([item["id"] for item in reports[5:]], decision["evidence"]["windows"][1]["report_ids"])
            self.assertTrue(all(
                group["previous_metrics"]["score"] - group["active_metrics"]["score"] >= 0.05
                for group in decision["evidence"]["windows"]
            ))
            self.assertEqual(decision, Store(path).latest_strategy_monitor_check())
            self.assertEqual(decision, service.status()["latest_monitor_check"])
            self.assertIsNone(service.maybe_auto_rollback(reports[-1]["id"]))

    def test_auto_rollback_needs_both_score_windows_and_rechecks_new_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "continue.db"
            store = Store(path)
            previous = store.active_strategy()
            candidate = store.save_strategy({**previous["config"], "numbered": False}, {}, "activated")
            store.activate_strategy(candidate["version"], source="evaluation")
            service = EvolutionService(store)
            for _ in range(5):
                add_case(store, path, "validation", NUMBERED, candidate["version"])
            for _ in range(4):
                add_case(store, path, "validation", BULLETED, candidate["version"])
            latest = add_case(store, path, "validation", BULLETED, candidate["version"])
            first = service.maybe_auto_rollback(latest["id"])
            self.assertEqual("kept", first["decision"])
            self.assertEqual(candidate["version"], store.active_strategy()["version"])
            self.assertEqual(first, service.maybe_auto_rollback(latest["id"]))
            latest = add_case(store, path, "validation", BULLETED, candidate["version"])
            second = service.maybe_auto_rollback(latest["id"])
            self.assertEqual("kept", second["decision"])
            self.assertNotEqual(first["id"], second["id"])

    def test_auto_rollback_skips_holdout_manual_activation_and_saved_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "isolation.db"
            store = Store(path)
            previous = store.active_strategy()
            candidate = store.save_strategy({**previous["config"], "numbered": False}, {}, "activated")
            store.activate_strategy(candidate["version"], source="evaluation")
            service = EvolutionService(store)
            source = {**SOURCE, "memory_context": {"preferences": {"numbered": True}}}
            reports = [add_case(store, path, "validation", NUMBERED, candidate["version"], source)
                       for _ in range(10)]
            holdout = add_case(store, path, "holdout", NUMBERED, candidate["version"])
            self.assertIsNone(service.maybe_auto_rollback(holdout["id"]))
            self.assertIsNone(store.latest_strategy_monitor_check())
            kept = service.maybe_auto_rollback(reports[-1]["id"])
            self.assertEqual("kept", kept["decision"])
            self.assertTrue(all(group["previous_metrics"] == group["active_metrics"]
                                for group in kept["evidence"]["windows"]))
            store.activate_strategy(previous["version"])
            store.activate_strategy(candidate["version"])
            latest = add_case(store, path, "validation", NUMBERED, candidate["version"])
            self.assertIsNone(service.maybe_auto_rollback(latest["id"]))
            self.assertIsNone(store.record_strategy_monitor_check(
                kept["activation_id"], candidate["version"], previous["version"],
                "stale-case", "rolled_back", "stale", kept["evidence"],
            ))
            self.assertEqual(candidate["version"], store.active_strategy()["version"])
            self.assertEqual("manual", store.latest_strategy_activation()["source"])

    def test_auto_rollback_on_safety_regression_even_without_score_drop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safety.db"
            store = Store(path)
            previous = store.active_strategy()
            candidate = store.save_strategy({**previous["config"], "numbered": False}, {}, "activated")
            store.activate_strategy(candidate["version"], source="evaluation")
            reports = [add_case(store, path, "validation", BULLETED, candidate["version"])
                       for _ in range(10)]
            service = EvolutionService(store)
            scores = [
                {"score": 0.9, "safety": 1.0}, {"score": 0.9, "safety": 1.0},
                {"score": 0.9, "safety": 1.0}, {"score": 0.9, "safety": 0.8},
            ]
            with patch.object(service, "_evaluate", side_effect=scores):
                decision = service.maybe_auto_rollback(reports[-1]["id"])
            self.assertEqual("rolled_back", decision["decision"])
            self.assertIn("安全性下降", decision["reason"])
            self.assertEqual(previous["version"], store.active_strategy()["version"])

    def test_confirmation_runs_monitor_without_losing_saved_report_on_monitor_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "confirm-monitor.db"
            client = TestClient(create_app(Settings("127.0.0.1", 8090, path)))
            store = client.app.state.store
            report = store.create_report("daily", "2026-10-14", SOURCE, NUMBERED, 1)
            with patch.object(client.app.state.evolution, "maybe_auto_rollback",
                              side_effect=RuntimeError("monitor")) as monitor, patch.object(
                client.app.state.evolution, "maybe_auto_rollback_writer_prompt",
                side_effect=RuntimeError("prompt monitor"),
            ) as prompt_monitor:
                response = client.post(f"/api/reports/{report['id']}/confirm", json={
                    "final_text": NUMBERED, "acknowledge_warnings": True,
                })
            self.assertEqual(200, response.status_code)
            monitor.assert_called_once_with(report["id"])
            prompt_monitor.assert_called_once_with(report["id"])
            self.assertEqual("confirmed", response.json()["status"])
            self.assertEqual("error", response.json()["post_activation_check"]["decision"])
            self.assertEqual("error", response.json()["writer_prompt_check"]["decision"])
            self.assertEqual("confirmed", store.get_report(report["id"])["status"])

    def test_post_activation_monitor_uses_only_fresh_generated_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.db"
            store = Store(path)
            previous = store.active_strategy()
            candidate = store.save_strategy({**previous["config"], "numbered": False}, {}, "activated")
            add_case(store, path, "validation", strategy_version=candidate["version"])
            self.assertTrue(store.activate_strategy(candidate["version"], source="evaluation"))
            activation = store.latest_strategy_activation()
            self.assertEqual((1, candidate["version"], "evaluation"), (
                activation["previous_version"], activation["active_version"], activation["source"],
            ))
            self.assertEqual(activation["id"], Store(path).latest_strategy_activation()["id"])
            service = EvolutionService(store)
            add_case(store, path, "holdout", strategy_version=candidate["version"])
            add_case(store, path, "validation", strategy_version=previous["version"])
            historical = store.import_historical_report("2026-10-14", SOURCE, BULLETED, candidate["version"])
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (historical["id"],))
            fresh = [add_case(store, path, "validation", strategy_version=candidate["version"])
                     for _ in range(4)]
            with patch.object(store, "list_evaluation_cases", wraps=store.list_evaluation_cases) as case_reads:
                monitor = service.status()["post_activation_monitor"]
            self.assertFalse(any(call.args[0] == "holdout" for call in case_reads.call_args_list))
            self.assertEqual("insufficient", monitor["status"])
            self.assertEqual(4, monitor["sample_count"])
            self.assertEqual({item["id"] for item in fresh}, set(monitor["report_ids"]))
            self.assertIsNone(monitor["active_metrics"])

            fresh.append(add_case(store, path, "validation", strategy_version=candidate["version"]))
            monitor = service.status()["post_activation_monitor"]
            self.assertEqual("ready", monitor["status"])
            self.assertEqual(5, monitor["sample_count"])
            self.assertGreater(monitor["active_metrics"]["score"], monitor["previous_metrics"]["score"])
            self.assertEqual(1.0, monitor["active_metrics"]["safety"])
            self.assertEqual({item["id"] for item in fresh}, set(monitor["report_ids"]))

            client = TestClient(create_app(Settings("127.0.0.1", 8090, path)))
            self.assertEqual("ready", client.get("/api/evolution/status").json()["post_activation_monitor"]["status"])
            self.assertTrue(store.activate_strategy(candidate["version"]))
            self.assertEqual(activation["id"], store.latest_strategy_activation()["id"])
            self.assertTrue(store.activate_strategy(previous["version"]))
            switched = store.latest_strategy_activation()
            self.assertEqual((candidate["version"], previous["version"], "manual"), (
                switched["previous_version"], switched["active_version"], switched["source"],
            ))
            self.assertEqual("insufficient", service.status()["post_activation_monitor"]["status"])
            self.assertEqual(0, service.status()["post_activation_monitor"]["sample_count"])

    def test_post_activation_monitor_applies_same_saved_preferences_to_both_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-preferences.db"
            store = Store(path)
            candidate = store.save_strategy({**store.active_strategy()["config"], "numbered": False}, {}, "activated")
            store.activate_strategy(candidate["version"], source="evaluation")
            source = {**SOURCE, "memory_context": {"preferences": {"numbered": True}}}
            for _ in range(5):
                add_case(store, path, "validation", NUMBERED, candidate["version"], source)
            monitor = EvolutionService(store).post_activation_monitor()
            self.assertEqual("ready", monitor["status"])
            self.assertEqual(monitor["previous_metrics"], monitor["active_metrics"])

    def test_benchmark_freezes_membership_and_uses_fresh_holdout_for_next_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            old_holdout = add_case(store, path, "holdout")
            service = EvolutionService(store)
            first = service.create_benchmark()
            self.assertEqual((3, 1), (len(first["validation_ids"]), len(first["holdout_ids"])))
            self.assertEqual(1, first["version"])

            add_case(store, path, "validation")
            new_holdout = add_case(store, path, "holdout")
            status = service.status()
            self.assertEqual(3, status["dataset"]["validation"])
            self.assertEqual(1, status["dataset"]["holdout"])
            self.assertEqual(3, status["writing_profile"]["sample_count"])
            self.assertEqual(1, status["benchmark"]["new_validation"])
            self.assertEqual(1, status["benchmark"]["new_holdout"])
            second = service.create_benchmark()
            self.assertEqual(2, second["version"])
            self.assertEqual(4, len(second["validation_ids"]))
            cases = store.list_evaluation_cases("holdout")
            old_case_id = next(item["id"] for item in cases if item["report_id"] == old_holdout["id"])
            new_case_id = next(item["id"] for item in cases if item["report_id"] == new_holdout["id"])
            self.assertEqual([new_case_id], second["holdout_ids"])
            self.assertNotIn(old_case_id, second["holdout_ids"])
            self.assertNotEqual(first["fingerprint"], second["fingerprint"])
            with self.assertRaises(ValueError):
                service.create_benchmark()

    def test_validation_failure_does_not_read_or_consume_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "validation.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation", NUMBERED)
            add_case(store, path, "holdout")
            service = EvolutionService(store)
            benchmark = service.create_benchmark()
            with patch.object(store, "list_evaluation_cases", wraps=store.list_evaluation_cases) as case_reads:
                service.status()
            self.assertTrue(all(call.args[0] != "holdout" for call in case_reads.call_args_list))
            original = store.benchmark_cases

            def read_validation_only(selected, split):
                self.assertEqual("validation", split)
                return original(selected, split)

            with patch.object(store, "benchmark_cases", side_effect=read_validation_only):
                result = service.propose()
            self.assertEqual("deferred", result["decision"])
            self.assertIsNone(result["metrics"]["candidate_holdout"])
            self.assertFalse(result["evidence"]["holdout_evaluated"])
            self.assertIsNone(store.list_evaluation_benchmarks()[0]["holdout_used_at"])
            self.assertEqual(benchmark["fingerprint"], store.list_evaluation_benchmarks()[0]["fingerprint"])

    def test_benchmark_api_shows_version_and_rejects_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.db"
            client = TestClient(create_app(Settings("127.0.0.1", 8090, path)))
            for _ in range(3):
                add_case(client.app.state.store, path, "validation")
            add_case(client.app.state.store, path, "holdout")
            created = client.post("/api/evolution/benchmarks")
            self.assertEqual(201, created.status_code)
            self.assertEqual(1, created.json()["version"])
            self.assertEqual(1, client.get("/api/evolution/status").json()["benchmark"]["version"])
            self.assertEqual(409, client.post("/api/evolution/benchmarks").status_code)

    def test_holdout_is_consumed_once_even_after_strategy_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "single-use.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            service = EvolutionService(store)
            first = service.propose()
            self.assertEqual("activated", first["decision"])
            self.assertTrue(first["evidence"]["holdout_evaluated"])
            self.assertIsNotNone(store.list_evaluation_benchmarks()[0]["holdout_used_at"])
            self.assertTrue(store.activate_strategy(1))
            second = service.propose()
            self.assertEqual("deferred", second["decision"])
            self.assertIsNone(second["metrics"]["current_holdout"])
            self.assertIn("已使用", second["reason"])
            self.assertFalse(second["evidence"]["holdout_evaluated"])

    def test_benchmark_rejects_modified_sample_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fingerprint.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            benchmark = EvolutionService(store).create_benchmark()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET expected_text='changed' WHERE id=?",
                                   (benchmark["validation_ids"][0],))
            with self.assertRaisesRegex(ValueError, "快照"):
                store.benchmark_cases(benchmark, "validation")

    def test_holdout_does_not_train_candidate_or_writing_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evolution.db"
            store = Store(path)
            source = {"kind": "daily", "date": "2026-10-13", "facts": [], "projects": []}
            validation = store.create_report("daily", "2026-10-13", source, "draft", 1)
            holdout = store.create_report("daily", "2026-10-13", source, "draft", 1)
            store.confirm_report(validation["id"], "## 今日完成\n- 验证集内容")
            store.confirm_report(holdout["id"], "## 今日完成\n1. 保留集内容")
            with closing(sqlite3.connect(path)) as connection:
                with connection:
                    connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (validation["id"],))
                    connection.execute("UPDATE evaluation_cases SET split='holdout' WHERE report_id=?", (holdout["id"],))

            service = EvolutionService(store)
            profile = service._writing_profile()
            result = service.propose()

        self.assertEqual(1, profile["sample_count"])
        self.assertFalse(result["candidate"]["config"]["numbered"])
        self.assertEqual("deferred", result["decision"])


if __name__ == "__main__":
    unittest.main()
