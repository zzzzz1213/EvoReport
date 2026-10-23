import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from evoreport.api import create_app
from evoreport.config import Settings


class FinalReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "review.db"
        self.client = TestClient(create_app(Settings("127.0.0.1", 8090, self.path)))
        self.store = self.client.app.state.store
        self.runtime = self.client.app.state.runtime
        self.report = self.client.post("/api/reports/daily", json={
            "report_date": "2026-10-16", "completed": "完成接口联调",
        }).json()
        self.final_text = self.report["draft"]

    def _fail_after_critic(self):
        with patch.object(self.store, "complete_final_review", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.runtime.confirm_final(
                    self.report["id"], self.report["source"], self.final_text,
                )
        run = self.store.list_agent_runs(1)[0]
        self.assertEqual("failed", run["status"])
        self.assertEqual("critic_ready", self.store.get_agent_checkpoint(run["id"])["stage"])
        return run

    def test_critic_checkpoint_resumes_same_run_and_confirms_once(self):
        run = self._fail_after_critic()
        self.assertEqual("draft", self.store.get_report(self.report["id"])["status"])
        self.assertEqual([run["id"]], [item["id"] for item in
                         self.client.get("/api/agents/recoverable").json()["runs"]])
        response = self.client.post(f"/api/agents/{run['id']}/resume", json={
            "final_text": self.final_text,
        })
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("confirmed", response.json()["status"])
        self.assertEqual(run["id"], response.json()["final_review"]["run_id"])
        completed = self.store.get_agent_run(run["id"])
        self.assertEqual(["Critic Agent", "Gate Agent"],
                         [step["agent_name"] for step in completed["steps"]])
        self.assertEqual(1, completed["recovery_count"])
        self.assertEqual("verified", self.client.get(f"/api/agents/{run['id']}/replay").json()["status"])
        self.assertNotIn("完成接口联调", str(self.store.get_agent_checkpoint(run["id"])))
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM evaluation_cases WHERE report_id=?", (self.report["id"],),
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_stale_prepared_run_can_be_taken_over(self):
        from evoreport.agent_runtime import _source_hash, _text_hash

        run_id = self.store.start_final_review(
            self.report["id"], _source_hash(self.report["source"]), _text_hash(self.final_text.strip()), False,
        )
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE agent_runs SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (run_id,),
            )
        runs = self.client.get("/api/agents/recoverable").json()["runs"]
        self.assertEqual([run_id], [item["id"] for item in runs])
        self.assertEqual("prepared", runs[0]["stage"])
        response = self.client.post(f"/api/agents/{run_id}/resume", json={"final_text": self.final_text})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("passed", self.store.get_agent_run(run_id)["status"])
        self.assertEqual(409, self.client.post(
            f"/api/agents/{run_id}/resume", json={"final_text": self.final_text},
        ).status_code)

    def test_changed_final_or_source_cannot_resume(self):
        run = self._fail_after_critic()
        changed = self.client.post(f"/api/agents/{run['id']}/resume", json={
            "final_text": self.final_text + "\n其他内容",
        })
        self.assertEqual(409, changed.status_code)
        self.assertEqual("failed", self.store.get_agent_run(run["id"])["status"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE reports SET source_json=? WHERE id=?",
                (json.dumps({"kind": "daily", "facts": ["不同事实"]}), self.report["id"]),
            )
        changed_source = self.client.post(f"/api/agents/{run['id']}/resume", json={
            "final_text": self.final_text,
        })
        self.assertEqual(409, changed_source.status_code)
        self.assertEqual("draft", self.store.get_report(self.report["id"])["status"])

    def test_recovery_stops_after_two_attempts(self):
        run = self._fail_after_critic()
        with patch.object(self.store, "complete_final_review", side_effect=RuntimeError("interrupted")):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    self.runtime.resume_final_review(run["id"], self.final_text)
        self.assertEqual(2, self.store.get_agent_run(run["id"])["recovery_count"])
        self.assertEqual([], self.runtime.recoverable_final_reviews())
        with self.assertRaisesRegex(ValueError, "超过恢复次数"):
            self.runtime.resume_final_review(run["id"], self.final_text)
        self.assertEqual("draft", self.store.get_report(self.report["id"])["status"])

    def test_late_confirmation_failure_rolls_back_gate_report_and_sample(self):
        with patch("evoreport.store.summarize_edit_feedback", side_effect=RuntimeError("late failure")):
            with self.assertRaisesRegex(RuntimeError, "late failure"):
                self.runtime.confirm_final(
                    self.report["id"], self.report["source"], self.final_text,
                )
        run = self.store.list_agent_runs(1)[0]
        self.assertEqual("failed", run["status"])
        self.assertEqual(["Critic Agent"], [step["agent_name"] for step in run["steps"]])
        self.assertEqual("draft", self.store.get_report(self.report["id"])["status"])
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM evaluation_cases WHERE report_id=?", (self.report["id"],),
            ).fetchone()[0]
        self.assertEqual(0, count)
        resumed = self.client.post(f"/api/agents/{run['id']}/resume", json={
            "final_text": self.final_text,
        })
        self.assertEqual(200, resumed.status_code, resumed.text)
        self.assertEqual("confirmed", resumed.json()["status"])


if __name__ == "__main__":
    unittest.main()
