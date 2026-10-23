import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from evoreport.agent_runtime import MultiAgentRuntime
from evoreport.evolution import EvolutionService
from evoreport.store import Store, _stratified_cases
from tests.test_agent_instructions import DRAFT, PromptWriter, add_case
from tests.test_prompt_evaluation_recovery import InterruptWriter


class StratifiedPromptEvaluationTests(unittest.TestCase):
    def test_selection_prefers_rare_scenarios_and_is_stable(self):
        cases = [{"id": str(index)} for index in range(5)]
        tags = {str(index): ["source:manual"] for index in range(5)}
        tags["4"].append("review:implicit")
        selected = _stratified_cases(cases, tags, 2)
        self.assertIn("4", [case["id"] for case in selected])
        self.assertEqual(selected, _stratified_cases(list(reversed(cases)), tags, 2))

    def test_frozen_scenarios_and_expanded_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stratified.db"
            store = Store(path)
            reports = []
            for index in range(7):
                source = {"kind": "daily", "facts": ["完成接口联调"],
                          "manual_completed": "完成接口联调" if index < 6 else "",
                          "projects": [{"id": "a"}, {"id": "b"}] if index == 6 else []}
                reports.append(add_case(store, path, "validation", source))
            for _ in range(2):
                add_case(store, path, "holdout")
            case = store.get_review_label_case(reports[6]["id"])
            store.save_review_labels(reports[6]["id"], [
                {"index": claim["index"], "label": "supported", "scenarios": ["implicit"]}
                for claim in case["claims"]
            ])
            writer = PromptWriter()
            service = EvolutionService(store, MultiAgentRuntime(store, writer))
            benchmark = service.create_benchmark()
            self.assertNotIn("scenarios_json", benchmark)
            self.assertEqual({"validation": 7, "holdout": 2}, benchmark["prompt_eligible_counts"])
            first = store.benchmark_prompt_cases(benchmark, "validation", 6)
            self.assertIn(reports[6]["id"], [item["report_id"] for item in first])
            self.assertEqual(first, store.benchmark_prompt_cases(benchmark, "validation", 6))
            coverage = store.benchmark_prompt_validation_coverage(benchmark, first)
            self.assertIn("review:implicit", coverage["selected"])
            store.save_review_labels(reports[6]["id"], [
                {"index": claim["index"], "label": "supported", "scenarios": ["paraphrase"]}
                for claim in case["claims"]
            ])
            self.assertEqual(coverage, store.benchmark_prompt_validation_coverage(benchmark, first))
            self.assertEqual(first, store.benchmark_prompt_cases(benchmark, "validation", 6))
            candidate = store.create_writer_prompt("表达简洁。")
            result = service.evaluate_writer_prompt(candidate["version"])
            self.assertEqual("passed", result["decision"])
            self.assertEqual(6, len(result["evidence"]["validation_cases"]))
            self.assertEqual(2, len(result["evidence"]["holdout_cases"]))
            self.assertEqual(16, len(writer.calls))
            self.assertTrue(store.list_evaluation_benchmarks()[0]["holdout_used_at"])

    def test_changed_scenario_snapshot_invalidates_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changed.db"
            store = Store(path)
            for _ in range(3):
                add_case(store, path, "validation")
            add_case(store, path, "holdout")
            benchmark = EvolutionService(store).create_benchmark()
            with closing(sqlite3.connect(path)) as connection, connection:
                raw = connection.execute("SELECT scenarios_json FROM evaluation_benchmarks").fetchone()[0]
                snapshot = json.loads(raw)
                next(iter(snapshot["validation"].values())).append("review:implicit")
                connection.execute("UPDATE evaluation_benchmarks SET scenarios_json=?", (json.dumps(snapshot),))
            with self.assertRaisesRegex(ValueError, "指纹不一致"):
                store.benchmark_prompt_cases(benchmark, "validation", 3)

    def test_expanded_holdout_recovers_without_reclaiming(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.db"
            store = Store(path)
            for _ in range(6):
                add_case(store, path, "validation")
            for _ in range(2):
                add_case(store, path, "holdout")
            runtime = MultiAgentRuntime(store, InterruptWriter(14))
            service = EvolutionService(store, runtime)
            service.create_benchmark()
            candidate = store.create_writer_prompt("表达简洁。")
            failed = service.evaluate_writer_prompt(candidate["version"])
            self.assertEqual("holdout_claimed", failed["stage"])
            runtime.writer = PromptWriter()
            completed = service.resume_writer_prompt_evaluation(failed["id"])
            self.assertEqual("passed", completed["decision"])
            self.assertEqual(17, completed["model_calls"])
            self.assertEqual(3, len(runtime.writer.calls))
            self.assertEqual(failed["id"], store.list_evaluation_benchmarks()[0]["holdout_run_id"])

    def test_critic_holdout_annotations_not_used_for_sampling(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review-holdout.db"
            store = Store(path)
            reports = [add_case(store, path, "validation") for _ in range(3)]
            add_case(store, path, "holdout")
            labeled = store.get_review_label_case(reports[0]["id"])
            store.save_review_labels(reports[0]["id"], [
                {"index": claim["index"], "label": "supported", "scenarios": ["implicit"]}
                for claim in labeled["claims"]
            ])
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("INSERT INTO critic_benchmarks(version,validation_fingerprint,created_at) VALUES(1,'x','now')")
                connection.execute("INSERT INTO critic_benchmark_members(version,report_id,split) VALUES(1,?,'holdout')",
                                   (reports[0]["id"],))
            benchmark = EvolutionService(store).create_benchmark()
            selected = store.benchmark_prompt_cases(benchmark, "validation", 3)
            coverage = store.benchmark_prompt_validation_coverage(benchmark, selected)
            self.assertNotIn("review:implicit", coverage["available"])


if __name__ == "__main__":
    unittest.main()
