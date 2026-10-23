import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from evoreport.agent_runtime import MultiAgentRuntime
from evoreport.api import create_app
from evoreport.config import Settings
from evoreport.evolution import EvolutionService
from evoreport.llm import ArkReportWriter
from evoreport.store import Store
from tests.test_evolution import add_case


class StrategyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "strategy-recovery.db"
        self.store = Store(self.path)
        for _ in range(3):
            add_case(self.store, self.path, "validation")
        add_case(self.store, self.path, "holdout")
        self.runtime = MultiAgentRuntime(self.store, ArkReportWriter("", "", ""))
        self.service = EvolutionService(self.store, self.runtime)

    def test_validation_checkpoint_resumes_frozen_candidate_before_holdout_claim(self):
        with patch.object(self.store, "claim_strategy_evaluation_holdout", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.service.propose()
        run = self.store.list_agent_runs(1)[0]
        checkpoint = self.store.get_strategy_evaluation_checkpoint(run["id"])
        self.assertEqual("validation_ready", checkpoint["stage"])
        self.assertIsNone(self.store.list_evaluation_benchmarks()[0]["holdout_used_at"])
        self.assertEqual([run["id"]], [item["id"] for item in self.service.recoverable_proposals()])
        with patch.object(self.service, "_derive_candidate", side_effect=AssertionError("不应重新生成候选")):
            result = self.service.resume_proposal(run["id"])
        self.assertEqual("activated", result["decision"])
        self.assertEqual(run["id"], result["trace_run_id"])
        self.assertEqual(1, self.store.get_agent_run(run["id"])["recovery_count"])
        self.assertEqual("completed", self.store.get_strategy_evaluation_checkpoint(run["id"])["stage"])
        self.assertEqual("verified", self.runtime.replay_report(run["id"])["status"])

    def test_holdout_claim_survives_interruption_without_second_claim(self):
        original = self.service._evaluate
        calls = 0

        def interrupt_first_holdout(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("interrupted")
            return original(*args, **kwargs)

        with patch.object(self.service, "_evaluate", side_effect=interrupt_first_holdout):
            with self.assertRaises(RuntimeError):
                self.service.propose()
        run_id = self.store.list_agent_runs(1)[0]["id"]
        self.assertEqual("holdout_claimed", self.store.get_strategy_evaluation_checkpoint(run_id)["stage"])
        benchmark = self.store.list_evaluation_benchmarks()[0]
        self.assertEqual(run_id, benchmark["holdout_run_id"])
        with patch.object(self.store, "claim_strategy_evaluation_holdout",
                          side_effect=AssertionError("不能再次领取 Holdout")):
            result = self.service.resume_proposal(run_id)
        self.assertEqual("activated", result["decision"])
        self.assertEqual(run_id, self.store.list_evaluation_benchmarks()[0]["holdout_run_id"])
        self.assertEqual(1, len(self.store.list_evaluation_runs()))
        self.assertEqual("evaluation", self.store.latest_strategy_activation()["source"])

    def test_stale_running_attempt_is_taken_over_and_fenced(self):
        with patch.object(self.store, "update_strategy_evaluation_checkpoint", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.propose()
        run = self.store.list_agent_runs(1)[0]
        self.assertEqual("running", run["status"])
        self.assertEqual("prepared", self.store.get_strategy_evaluation_checkpoint(run["id"])["stage"])
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE agent_runs SET updated_at=? WHERE id=?", (old_time, run["id"]))
        self.assertEqual([run["id"]], [item["id"] for item in self.service.recoverable_proposals()])
        result = self.service.resume_proposal(run["id"])
        self.assertEqual("activated", result["decision"])
        with self.assertRaises(ValueError):
            self.store.update_strategy_evaluation_checkpoint(
                run["id"], 0, "prepared", "validation_ready", {}, {}, 0,
            )
        self.assertEqual("completed", self.store.get_strategy_evaluation_checkpoint(run["id"])["stage"])

    def test_decision_checkpoint_resumes_without_rescoring_and_api_exposes_run(self):
        with patch.object(self.store, "finalize_strategy_evaluation", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.service.propose()
        run_id = self.store.list_agent_runs(1)[0]["id"]
        self.assertEqual("decision_ready", self.store.get_strategy_evaluation_checkpoint(run_id)["stage"])
        app = create_app(Settings("127.0.0.1", 8090, self.path))
        client = TestClient(app)
        runs = client.get("/api/agents/recoverable").json()["runs"]
        self.assertEqual([run_id], [item["id"] for item in runs if item["workflow"] == "strategy_evaluation"])
        with patch.object(app.state.evolution, "_evaluate", side_effect=AssertionError("不应重新评分")):
            response = client.post(f"/api/agents/{run_id}/resume")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("activated", response.json()["decision"])
        self.assertEqual([], app.state.evolution.recoverable_proposals())

    def test_stale_baseline_cannot_activate_old_candidate(self):
        with patch.object(self.store, "claim_strategy_evaluation_holdout", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.service.propose()
        old_run = self.store.list_agent_runs(1)[0]["id"]
        winner = self.service.propose()
        self.assertEqual("activated", winner["decision"])
        with self.assertRaisesRegex(ValueError, "基线策略已变化"):
            self.service.resume_proposal(old_run)
        self.assertEqual("invalidated", self.store.get_agent_run(old_run)["status"])
        self.assertEqual(winner["candidate"]["version"], self.store.active_strategy()["version"])
        self.assertEqual(1, len(self.store.list_evaluation_runs()))

    def test_final_commit_rolls_back_all_results_on_late_error(self):
        with patch.object(self.store, "finalize_strategy_evaluation", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.service.propose()
        run_id = self.store.list_agent_runs(1)[0]["id"]
        checkpoint = self.store.get_strategy_evaluation_checkpoint(run_id)
        checkpoint["evidence"]["rules"] = [{"after": True}]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE strategy_evaluation_checkpoints SET evidence_json=? WHERE run_id=?",
                               (json.dumps(checkpoint["evidence"]), run_id))
        with self.assertRaises(KeyError):
            self.service.resume_proposal(run_id)
        self.assertEqual(1, len(self.store.list_strategies()))
        self.assertEqual([], self.store.list_evaluation_runs())
        self.assertEqual(1, self.store.active_strategy()["version"])
        self.assertEqual([], self.store.get_agent_run(run_id)["steps"])


if __name__ == "__main__":
    unittest.main()
