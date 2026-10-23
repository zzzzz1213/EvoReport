import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from evoreport.api import create_app
from evoreport.config import Settings
from evoreport.evaluation import review_report
from evoreport.store import Store


FINAL_TEXT = (
    "# 工作日报\n\n## 今日完成\n"
    "- 完成接口联调\n- 完成接口联调与冒烟测试\n"
    "- 修复登录异常\n- 发布生产环境\n\n"
    "## 遇到的问题\n- 暂无\n\n## 明日计划\n- 补充测试\n"
)
SOURCE = {"kind": "daily", "facts": ["完成接口联调", "修复登录异常"], "projects": []}


class CriticRuleEvaluationTests(unittest.TestCase):
    def test_match_threshold_is_bounded(self):
        with self.assertRaises(ValueError):
            review_report(SOURCE, FINAL_TEXT, 81)
        with self.assertRaises(ValueError):
            review_report(SOURCE, FINAL_TEXT, True)
        with self.assertRaises(ValueError):
            review_report(SOURCE, FINAL_TEXT, 60, 51)
        with self.assertRaises(ValueError):
            review_report(SOURCE, FINAL_TEXT, 60, 0, 61)
        with self.assertRaises(ValueError):
            review_report(SOURCE, FINAL_TEXT, 60, 0, 60, 21)

    def test_skill_is_declarative_and_validation_can_propose_a_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "skill.db")))
            store = client.app.state.store
            for index, day in enumerate(("2026-10-01", "2026-10-02", "2026-10-03")):
                source = {**SOURCE, "projects": [{
                    "id": "alpha" if index != 1 else "beta",
                    "name": "alpha" if index != 1 else "beta",
                }]}
                report = store.create_report("daily", day, source, FINAL_TEXT, 1)
                store.confirm_report(report["id"], FINAL_TEXT)
                with closing(sqlite3.connect(store.path)) as connection, connection:
                    connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?",
                                       (report["id"],))
                claims = client.get(f"/api/reports/{report['id']}/review-labels").json()["claims"]
                labels = [{"index": item["index"],
                           "label": "unsupported" if item["index"] in (1, 3) else "supported",
                           "scenarios": ["paraphrase"] if item["index"] == 1 else []}
                          for item in claims]
                self.assertEqual(200, client.put(
                    f"/api/reports/{report['id']}/review-labels", json={"labels": labels},
                ).status_code)
            self.assertEqual(201, client.post("/api/agents/critic-benchmarks").status_code)
            proposed = client.post("/api/agents/critic-skills/propose")
            self.assertEqual(201, proposed.status_code, proposed.text)
            result = proposed.json()
            self.assertEqual("validation_proposal", result["version"]["source"])
            self.assertEqual(2, result["version"]["skill"]["schema_version"])
            self.assertGreater(result["version"]["skill"]["claim_match"]["semantic_weight_percent"], 0)
            self.assertGreater(result["version"]["skill"]["claim_match"]["uncertainty_margin_percent"], 0)
            self.assertEqual([
                "missing_facts", "required_headings", "sensitive_information", "unsupported_claims",
            ], result["version"]["skill"]["required_checks"])
            self.assertEqual("improved", result["evaluation"]["decision"])
            self.assertEqual(3, len(result["version"]["proposal"]["validation_report_ids"]))
            self.assertGreater(result["version"]["proposal"]["searched_candidates"], 0)
            self.assertNotIn(FINAL_TEXT, json.dumps(result, ensure_ascii=False))
            listed = client.get("/api/agents/critic-rules").json()["versions"][0]
            self.assertEqual(result["version"]["fingerprint"], listed["fingerprint"])

    def test_semantic_skill_schema_is_strict_and_versioned(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "semantic.db")))
            created = client.post("/api/agents/critic-rules", json={
                "claim_threshold_percent": 65, "ngram_overlap_weight_percent": 25,
                "semantic_weight_percent": 60, "uncertainty_margin_percent": 10,
            })
            self.assertEqual(201, created.status_code, created.text)
            skill = created.json()["skill"]
            self.assertEqual(2, skill["schema_version"])
            self.assertEqual(60, skill["claim_match"]["semantic_weight_percent"])
            self.assertEqual(10, skill["claim_match"]["uncertainty_margin_percent"])

    def test_skill_json_rejects_unknown_or_disabled_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "guard.db")))
            store = client.app.state.store
            created = client.post("/api/agents/critic-rules", json={
                "claim_threshold_percent": 70, "ngram_overlap_weight_percent": 25,
            })
            self.assertEqual(201, created.status_code, created.text)
            skill = created.json()["skill"]
            self.assertEqual(25, skill["claim_match"]["ngram_overlap_weight_percent"])
            skill["required_checks"].remove("sensitive_information")
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE critic_rule_versions SET skill_json=? WHERE version=?",
                                   (json.dumps(skill), created.json()["version"]))
            response = client.post(f"/api/agents/critic-rules/{created.json()['version']}/evaluate")
            self.assertEqual(409, response.status_code)
            self.assertIn("不能关闭", response.json()["detail"])

    def test_legacy_critic_rule_table_gets_equivalent_skill_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-skill.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("""CREATE TABLE critic_rule_versions (
                    version INTEGER PRIMARY KEY,claim_threshold_percent INTEGER NOT NULL,
                    algorithm_version INTEGER NOT NULL,fingerprint TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(claim_threshold_percent,algorithm_version)
                )""")
            store = Store(path)
            rule = store.active_critic_rule()
            self.assertEqual(0, rule["skill"]["schema_version"])
            self.assertEqual(60, rule["skill"]["claim_match"]["threshold_percent"])
            with closing(sqlite3.connect(path)) as connection:
                columns = {row[1] for row in connection.execute(
                    "PRAGMA table_info(critic_rule_versions)"
                )}
            self.assertIn("skill_json", columns)
            self.assertIn("proposal_json", columns)

    def test_candidate_is_versioned_and_compared_only_with_labeled_validation_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "critic.db")))
            store = client.app.state.store
            created = client.post("/api/agents/critic-rules", json={"claim_threshold_percent": 80})
            self.assertEqual(201, created.status_code, created.text)
            version = created.json()["version"]
            self.assertEqual(409, client.post("/api/agents/critic-rules", json={
                "claim_threshold_percent": 80,
            }).status_code)
            self.assertEqual(409, client.post("/api/agents/critic-rules", json={
                "claim_threshold_percent": 60,
            }).status_code)
            self.assertEqual(422, client.post("/api/agents/critic-rules", json={
                "claim_threshold_percent": 81,
            }).status_code)
            evaluate = f"/api/agents/critic-rules/{version}/evaluate"
            self.assertEqual(409, client.post(evaluate).status_code)

            report_ids = []
            for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
                report = store.create_report("daily", day, SOURCE, FINAL_TEXT, 1)
                store.confirm_report(report["id"], FINAL_TEXT)
                with closing(sqlite3.connect(store.path)) as connection, connection:
                    connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (
                        report["id"],
                    ))
                report_ids.append(report["id"])
                case = client.get(f"/api/reports/{report['id']}/review-labels").json()
                self.assertEqual(4, len(case["claims"]))
                labels = [{"index": item["index"], "label": "unsupported" if item["index"] in (1, 3)
                           else "supported"} for item in case["claims"]]
                self.assertEqual(200, client.put(f"/api/reports/{report['id']}/review-labels", json={
                    "labels": labels,
                }).status_code)

            for report_id in report_ids:
                self.assertEqual(200, client.put(f"/api/reports/{report_id}/review-labels", json={
                    "labels": [{"index": index, "label": "supported"} for index in range(4)],
                }).status_code)
            self.assertEqual("ready", client.get("/api/review-benchmark").json()["status"])
            self.assertEqual(409, client.post(evaluate).status_code)
            for report_id in report_ids:
                self.assertEqual(200, client.put(f"/api/reports/{report_id}/review-labels", json={
                    "labels": [{"index": index, "label": "unsupported" if index in (1, 3)
                                else "supported"} for index in range(4)],
                }).status_code)

            baseline_gate = review_report(SOURCE, FINAL_TEXT)["unsupported_claims"]
            result = client.post(evaluate)
            self.assertEqual(200, result.status_code, result.text)
            data = result.json()
            self.assertEqual("improved", data["decision"])
            self.assertEqual(3, data["baseline"]["counts"]["false_negative"])
            self.assertEqual(0, data["candidate"]["counts"]["false_negative"])
            self.assertEqual(0, data["candidate"]["counts"]["false_positive"])
            self.assertEqual(3, len(data["changes"]))
            self.assertTrue(all(item["before"] == "false_negative" and
                                item["after"] == "true_positive" for item in data["changes"]))
            self.assertEqual(baseline_gate, review_report(SOURCE, FINAL_TEXT)["unsupported_claims"])
            self.assertNotIn("完成接口联调与冒烟测试", json.dumps(data, ensure_ascii=False))
            self.assertEqual(data["id"], client.post(evaluate).json()["id"])
            weaker = client.post("/api/agents/critic-rules", json={"claim_threshold_percent": 40})
            self.assertEqual(201, weaker.status_code)
            weaker_result = client.post(
                f"/api/agents/critic-rules/{weaker.json()['version']}/evaluate"
            )
            self.assertEqual(200, weaker_result.status_code, weaker_result.text)
            self.assertEqual("not_improved", weaker_result.json()["decision"])

            old_fingerprint = data["corpus_fingerprint"]
            case = client.get(f"/api/reports/{report_ids[0]}/review-labels").json()
            labels = [{"index": item["index"], "label": "supported"} for item in case["claims"]]
            self.assertEqual(200, client.put(f"/api/reports/{report_ids[0]}/review-labels", json={
                "labels": labels,
            }).status_code)
            listing = client.get("/api/agents/critic-rules").json()
            self.assertNotEqual(old_fingerprint, listing["corpus_fingerprint"])
            self.assertTrue(listing["evaluations"][0]["stale"])
            rerun = client.post(evaluate)
            self.assertEqual(200, rerun.status_code, rerun.text)
            self.assertNotEqual(data["id"], rerun.json()["id"])

    def test_holdout_labels_do_not_enter_critic_candidate_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "holdout.db")))
            store = client.app.state.store
            candidate = client.post("/api/agents/critic-rules", json={"claim_threshold_percent": 70}).json()
            report = store.create_report("daily", "2026-10-04", SOURCE, FINAL_TEXT, 1)
            store.confirm_report(report["id"], FINAL_TEXT)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET split='holdout' WHERE report_id=?", (report["id"],))
            self.assertEqual(409, client.get(f"/api/reports/{report['id']}/review-labels").status_code)
            self.assertEqual(0, client.get("/api/agents/critic-rules").json()["benchmark"]["labeled_claims"])
            self.assertEqual(409, client.post(
                f"/api/agents/critic-rules/{candidate['version']}/evaluate"
            ).status_code)


if __name__ == "__main__":
    unittest.main()
