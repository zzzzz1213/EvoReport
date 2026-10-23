import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from evoreport.api import create_app
from evoreport.config import Settings


TEXT = (
    "# 工作日报\n\n## 今日完成\n"
    "- 完成接口联调\n- 完成接口联调与冒烟测试\n"
    "- 修复登录异常\n- 发布生产环境\n\n"
    "## 遇到的问题\n- 暂无\n\n## 明日计划\n- 补充测试\n"
)


class CriticBenchmarkTests(unittest.TestCase):
    def test_scenario_tags_preserve_legacy_fingerprint_and_keep_holdout_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "tags.db")))
            validation = [self._report(client, f"2026-10-0{day}", "alpha" if day != 2 else "beta")
                          for day in (1, 2, 3)]
            before = client.get("/api/review-benchmark").json()
            self.assertEqual(201, client.post("/api/agents/critic-benchmarks").status_code)
            store = client.app.state.store
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("ALTER TABLE review_claim_labels DROP COLUMN scenarios_json")
                connection.execute("ALTER TABLE review_missed_claims DROP COLUMN scenarios_json")
            from evoreport.store import Store
            migrated = Store(store.path)
            self.assertEqual(before["corpus_fingerprint"], migrated.review_benchmark()["corpus_fingerprint"])
            self.assertFalse(migrated.critic_benchmark_status()["validation_stale"])
            holdout = self._report(client, "2026-10-04", "alpha")
            path = f"/api/reports/{holdout}/review-labels"
            claims = client.get(path).json()["claims"]
            labels = [{"index": item["index"], "label": item["label"], "scenarios": ["implicit"]}
                      for item in claims]
            self.assertEqual(200, client.put(path, json={"labels": labels}).status_code)
            after = client.get("/api/review-benchmark").json()
            self.assertEqual(before["scenario_coverage"], after["scenario_coverage"])
            self.assertEqual(before["corpus_fingerprint"], after["corpus_fingerprint"])
            validation_path = f"/api/reports/{validation[0]}/review-labels"
            claims = client.get(validation_path).json()["claims"]
            labels = [{"index": item["index"], "label": item["label"],
                       "scenarios": ["compound"] if item["index"] == 0 else []} for item in claims]
            self.assertEqual(200, client.put(validation_path, json={"labels": labels}).status_code)
            self.assertTrue(store.critic_benchmark_status()["validation_stale"])

    @staticmethod
    def _report(client, day, project_id):
        store = client.app.state.store
        source = {"kind": "daily", "facts": ["完成接口联调", "修复登录异常"],
                  "projects": [{"id": project_id, "name": project_id}]}
        report = store.create_report("daily", day, source, TEXT, 1)
        store.confirm_report(report["id"], TEXT)
        with closing(sqlite3.connect(store.path)) as connection, connection:
            connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?",
                               (report["id"],))
        claims = client.get(f"/api/reports/{report['id']}/review-labels").json()["claims"]
        labels = [{"index": claim["index"], "label": "unsupported" if claim["index"] in (1, 3)
                   else "supported"} for claim in claims]
        response = client.put(f"/api/reports/{report['id']}/review-labels", json={"labels": labels})
        assert response.status_code == 200, response.text
        return report["id"]

    def test_frozen_validation_and_one_use_holdout_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "critic.db")))
            candidate = client.post("/api/agents/critic-rules", json={
                "claim_threshold_percent": 80,
            }).json()["version"]
            self.assertEqual(409, client.post("/api/agents/critic-benchmarks").status_code)
            for day, project in (("2026-10-01", "alpha"), ("2026-10-02", "beta"),
                                 ("2026-10-03", "alpha")):
                self._report(client, day, project)
            frozen = client.post("/api/agents/critic-benchmarks")
            self.assertEqual(201, frozen.status_code, frozen.text)
            self.assertEqual("collecting", frozen.json()["state"])
            self.assertEqual(409, client.post("/api/agents/critic-benchmarks").status_code)
            validation = client.post(f"/api/agents/critic-rules/{candidate}/evaluate")
            self.assertEqual(200, validation.status_code, validation.text)
            self.assertEqual("improved", validation.json()["decision"])
            fingerprint = validation.json()["corpus_fingerprint"]
            self.assertEqual(409, client.post(f"/api/agents/critic-rules/{candidate}/gate").status_code)

            first_holdout = self._report(client, "2026-10-04", "alpha")
            self.assertEqual(fingerprint, client.get("/api/agents/critic-rules").json()["corpus_fingerprint"])
            self.assertEqual(409, client.post(f"/api/agents/critic-rules/{candidate}/gate").status_code)
            second_holdout = self._report(client, "2026-10-05", "beta")
            listing = client.get("/api/agents/critic-rules").json()
            self.assertEqual(fingerprint, listing["corpus_fingerprint"])
            self.assertEqual(2, listing["frozen_benchmark"]["holdout_reports"])
            self.assertTrue(listing["frozen_benchmark"]["holdout_ready"])
            self.assertNotIn("baseline_counts", str(listing["frozen_benchmark"]))
            gated = client.post(f"/api/agents/critic-rules/{candidate}/gate")
            self.assertEqual(200, gated.status_code, gated.text)
            self.assertEqual("passed", gated.json()["decision"])
            self.assertEqual(409, client.post(f"/api/agents/critic-rules/{candidate}/gate").status_code)
            self.assertEqual("used", client.get("/api/agents/critic-rules").json()["frozen_benchmark"]["state"])
            self.assertEqual(409, client.post("/api/agents/critic-benchmarks").status_code)
            self.assertIn(first_holdout, client.app.state.store.get_review_label_case(first_holdout)["report_id"])
            self.assertNotEqual(first_holdout, second_holdout)

    def test_changed_validation_blocks_gate_without_consuming_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "stale.db")))
            candidate = client.post("/api/agents/critic-rules", json={
                "claim_threshold_percent": 80,
            }).json()["version"]
            validation_ids = [self._report(client, f"2026-10-0{day}", "alpha" if day % 2 else "beta")
                              for day in (1, 2, 3)]
            self.assertEqual(201, client.post("/api/agents/critic-benchmarks").status_code)
            self.assertEqual(200, client.post(f"/api/agents/critic-rules/{candidate}/evaluate").status_code)
            self._report(client, "2026-10-04", "alpha")
            self._report(client, "2026-10-05", "beta")
            changed = [{"index": index, "label": "supported"} for index in range(4)]
            self.assertEqual(200, client.put(f"/api/reports/{validation_ids[0]}/review-labels",
                                             json={"labels": changed}).status_code)
            self.assertEqual(409, client.post(f"/api/agents/critic-rules/{candidate}/gate").status_code)
            self.assertEqual("collecting", client.get("/api/agents/critic-rules").json()[
                "frozen_benchmark"]["state"])

    def test_manual_activation_rollback_replay_and_post_activation_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "rollout.db")))
            store = client.app.state.store
            candidate = client.post("/api/agents/critic-rules", json={
                "claim_threshold_percent": 80,
            }).json()["version"]
            other = client.post("/api/agents/critic-rules", json={
                "claim_threshold_percent": 70,
            }).json()["version"]
            self.assertEqual(409, client.post(f"/api/agents/critic-rules/{candidate}/activate").status_code)
            for day, project in (("2026-10-01", "alpha"), ("2026-10-02", "beta"),
                                 ("2026-10-03", "alpha")):
                self._report(client, day, project)
            self.assertEqual(201, client.post("/api/agents/critic-benchmarks").status_code)
            self.assertEqual("improved", client.post(
                f"/api/agents/critic-rules/{candidate}/evaluate").json()["decision"])
            validation_fingerprint = client.get("/api/agents/critic-rules").json()["corpus_fingerprint"]
            self._report(client, "2026-10-04", "alpha")
            self._report(client, "2026-10-05", "beta")
            self.assertEqual("passed", client.post(
                f"/api/agents/critic-rules/{candidate}/gate").json()["decision"])
            self.assertEqual(409, client.post(f"/api/agents/critic-rules/{other}/activate").status_code)
            activated = client.post(f"/api/agents/critic-rules/{candidate}/activate")
            self.assertEqual(200, activated.status_code, activated.text)
            self.assertEqual(candidate, activated.json()["version"])
            after_activation = client.get("/api/agents/critic-rules").json()
            self.assertEqual(candidate, after_activation["active_version"])
            self.assertEqual(validation_fingerprint, after_activation["corpus_fingerprint"])
            self.assertFalse(after_activation["evaluations"][0]["stale"])

            source = {"kind": "daily", "facts": ["完成接口联调", "修复登录异常"],
                      "projects": [{"id": "alpha", "name": "alpha"}]}
            draft_report = store.create_report("daily", "2026-10-06", source, TEXT, 1)
            daily_run = client.app.state.runtime.run_report(draft_report["id"], source, TEXT)
            daily_binding = store.get_agent_run(daily_run["run_id"])["instruction_snapshot"]["critic_rules"]
            self.assertEqual(candidate, daily_binding["version"])
            self.assertEqual("verified", client.get(
                f"/api/agents/{daily_run['run_id']}/replay").json()["status"])
            report = store.create_report("daily", "2026-10-06", source, TEXT, 1)
            result = client.app.state.runtime.confirm_final(report["id"], source, TEXT, True)
            self.assertEqual("passed", result["decision"])
            run_id = result["run_id"]
            binding = store.get_agent_run(run_id)["instruction_snapshot"]["critic_rules"]
            self.assertEqual(candidate, binding["version"])
            self.assertEqual(activated.json()["fingerprint"], binding["fingerprint"])
            self.assertEqual("verified", client.get(f"/api/agents/{run_id}/replay").json()["status"])

            for day, project in (("2026-10-07", "alpha"), ("2026-10-08", "beta"),
                                 ("2026-10-09", "alpha")):
                self._report(client, day, project)
            monitor = client.get("/api/agents/critic-rules").json()["monitor"]
            self.assertEqual("ready", monitor["status"])
            self.assertEqual(3, monitor["report_count"])
            self.assertLess(monitor["counts"]["false_negative"],
                            monitor["previous_counts"]["false_negative"])

            paused = store.create_report("daily", "2026-10-10", source, TEXT, 1)
            with patch.object(store, "complete_final_review", side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    client.app.state.runtime.confirm_final(paused["id"], source, TEXT, True)
            paused_run = store.list_agent_runs(1)[0]
            self.assertEqual(candidate, paused_run["instruction_snapshot"]["critic_rules"]["version"])
            self.assertEqual(1, client.post("/api/agents/critic-rules/1/activate").json()["version"])
            self.assertEqual("verified", client.get(f"/api/agents/{run_id}/replay").json()["status"])
            self.assertEqual("inactive", client.get("/api/agents/critic-rules").json()["monitor"]["status"])
            self.assertEqual("passed", client.app.state.runtime.resume_final_review(
                paused_run["id"], TEXT)["decision"])
            self.assertEqual("verified", client.get(f"/api/agents/{paused_run['id']}/replay").json()["status"])
            self.assertEqual(candidate, client.post(
                f"/api/agents/critic-rules/{candidate}/activate").json()["version"])
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE critic_rule_versions SET fingerprint='bad' WHERE version=?",
                                   (candidate,))
            replay = client.get(f"/api/agents/{run_id}/replay").json()
            self.assertEqual("unavailable", replay["status"])


if __name__ == "__main__":
    unittest.main()
