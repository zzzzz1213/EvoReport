import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from evoreport.api import create_app
from evoreport.config import Settings


FINAL_TEXT = (
    "# 工作日报\n\n## 今日完成\n"
    "- 完成接口联调\n- 完成接口联调与异常路径覆盖\n"
    "- 完成未经记录的生产发布\n- 修复登录异常\n\n"
    "## 遇到的问题\n- 暂无\n\n## 明日计划\n- 补充测试\n"
)


class ReviewBenchmarkTests(unittest.TestCase):
    def test_legacy_review_tables_add_claim_snapshot_column_before_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-review.db"
            client = TestClient(create_app(Settings("127.0.0.1", 8090, path)))
            store = client.app.state.store
            report = store.create_report("daily", "2026-10-01", {
                "kind": "daily", "facts": ["完成接口联调"], "projects": [],
            }, FINAL_TEXT, 1)
            store.confirm_report(report["id"], FINAL_TEXT)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],))
                connection.execute("ALTER TABLE review_claim_labels DROP COLUMN claims_sha256")
                connection.execute("ALTER TABLE review_missed_claims DROP COLUMN claims_sha256")

            migrated_client = TestClient(create_app(Settings("127.0.0.1", 8090, path)))
            with closing(sqlite3.connect(path)) as connection:
                for table in ("review_claim_labels", "review_missed_claims"):
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    self.assertIn("claims_sha256", columns)

            endpoint = f"/api/reports/{report['id']}/review-labels"
            claims = migrated_client.get(endpoint).json()["claims"]
            saved = migrated_client.put(endpoint, json={"labels": [
                {"index": item["index"], "label": "supported"} for item in claims
            ]})
            self.assertEqual(200, saved.status_code, saved.text)
            self.assertTrue(saved.json()["labeled"])

    def test_scenarios_count_parent_once_and_preserve_manual_tags(self):
        final_text = "# 工作日报\n\n## 今日完成\n- 完成接口联调顺带修订上线方案\n今天补充了部署说明\n\n## 遇到的问题\n暂无\n"
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "scenario.db")))
            store = client.app.state.store
            report = store.create_report("daily", "2026-10-04", {
                "kind": "daily", "facts": ["完成接口联调"], "projects": [{"id": "p1"}],
            }, final_text, 1)
            store.confirm_report(report["id"], final_text)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],))
            path = f"/api/reports/{report['id']}/review-labels"
            case = client.get(path).json()
            self.assertEqual(1, len(case["claims"]))
            parent = case["claims"][0]
            lines = final_text.splitlines()
            nested_text = "修订上线方案"
            nested_start = lines[3].index(nested_text)
            separate_text = lines[4]
            payload = {"labels": [{"index": 0, "label": "unsupported", "scenarios": ["paraphrase"]}],
                       "missed_claims": [
                           {"line": 4, "column_start": nested_start, "column_end": nested_start + len(nested_text),
                            "scenarios": ["compound", "paraphrase"]},
                           {"line": 5, "column_start": 0, "column_end": len(separate_text),
                            "scenarios": ["implicit"]},
                       ]}
            self.assertEqual(200, client.put(path, json=payload).status_code)
            saved = client.get(path).json()
            self.assertEqual(["paraphrase"], saved["claims"][0]["scenarios"])
            self.assertEqual(["compound", "paraphrase"], saved["missed_claims"][0]["scenarios"])
            benchmark = client.get("/api/review-benchmark").json()
            coverage = benchmark["scenario_coverage"]
            self.assertEqual(2, benchmark["labeled_claims"])
            self.assertEqual(1, benchmark["project_count"])
            self.assertEqual(1, coverage["scenarios"]["compound"]["items"])
            self.assertEqual(1, coverage["scenarios"]["paraphrase"]["items"])
            self.assertEqual(1, coverage["scenarios"]["implicit"]["false_negative"])
            self.assertEqual(0, coverage["untagged"])
            duplicate = {**payload, "labels": [{"index": 0, "label": "unsupported",
                                               "scenarios": ["compound", "compound"]}]}
            self.assertEqual(409, client.put(path, json=duplicate).status_code)
            self.assertEqual(saved, client.get(path).json())
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE reports SET final_text=? WHERE id=?", (final_text + "\n", report["id"]))
            self.assertTrue(client.get(path).json()["stale"])
            self.assertEqual(0, client.get("/api/review-benchmark").json()["scenario_coverage"]["scenarios"]["implicit"]["items"])

    def test_labels_measure_false_positive_and_false_negative_only_after_enough_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "benchmark.db")))
            store = client.app.state.store
            report_ids = []
            for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
                source = {"kind": "daily", "facts": ["完成接口联调"],
                          "manual_completed": "完成接口联调", "projects": []}
                report = store.create_report("daily", day, source, FINAL_TEXT, 1)
                store.confirm_report(report["id"], FINAL_TEXT)
                with closing(sqlite3.connect(store.path)) as connection, connection:
                    connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],))
                report_ids.append(report["id"])

            first = client.get(f"/api/reports/{report_ids[0]}/review-labels")
            self.assertEqual(200, first.status_code, first.text)
            claims = first.json()["claims"]
            self.assertEqual(4, len(claims))
            self.assertTrue(any(item["flagged"] for item in claims))
            self.assertTrue(any(not item["flagged"] for item in claims))
            self.assertEqual(409, client.put(f"/api/reports/{report_ids[0]}/review-labels", json={
                "labels": [{"index": 0, "label": "supported"}],
            }).status_code)
            self.assertEqual(409, client.put(f"/api/reports/{report_ids[0]}/review-labels", json={
                "labels": [{"index": item["index"], "label": "supported"} for item in claims],
                "missed_claims": [{"line": claims[0]["line"], "column_start": claims[0]["column_start"],
                                   "column_end": claims[0]["column_end"]}],
            }).status_code)
            self.assertEqual(0, store.review_benchmark()["labeled_claims"])

            for report_id in report_ids:
                case = client.get(f"/api/reports/{report_id}/review-labels").json()
                labels = [{"index": item["index"],
                           "label": "unsupported" if item["flagged"] else "supported"}
                          for item in case["claims"]]
                labels[0]["label"] = "unsupported"
                flagged_index = next(item["index"] for item in case["claims"] if item["flagged"])
                labels[flagged_index]["label"] = "supported"
                saved = client.put(f"/api/reports/{report_id}/review-labels", json={"labels": labels})
                self.assertEqual(200, saved.status_code, saved.text)
                self.assertTrue(saved.json()["labeled"])
                if report_id == report_ids[0]:
                    self.assertEqual("insufficient", client.get("/api/review-benchmark").json()["status"])

            result = client.get("/api/review-benchmark").json()
            self.assertEqual("ready", result["status"])
            self.assertEqual(3, result["report_count"])
            self.assertEqual(12, result["labeled_claims"])
            self.assertEqual(3, result["counts"]["false_positive"])
            self.assertEqual(3, result["counts"]["false_negative"])
            self.assertEqual(12, sum(result["decision_counts"].values()))
            self.assertEqual(0, result["decision_counts"]["uncertain"])
            self.assertEqual(6, len(result["mismatches"]))
            self.assertIsNotNone(result["precision"])
            self.assertIsNotNone(result["recall"])

            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE reports SET final_text=? WHERE id=?", (FINAL_TEXT + "\n", report_ids[0]))
            stale = client.get(f"/api/reports/{report_ids[0]}/review-labels").json()
            self.assertTrue(stale["stale"])
            self.assertTrue(all(item["label"] is None for item in stale["claims"]))
            after = client.get("/api/review-benchmark").json()
            self.assertEqual(1, after["stale_reports"])
            self.assertEqual(2, after["report_count"])
            self.assertEqual("insufficient", after["status"])

    def test_holdout_cannot_be_labeled_and_source_change_invalidates_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "review.db")))
            store = client.app.state.store
            report = store.create_report("daily", "2026-10-01", {
                "kind": "daily", "facts": ["完成接口联调"], "projects": [],
            }, FINAL_TEXT, 1)
            store.confirm_report(report["id"], FINAL_TEXT)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET split='holdout' WHERE report_id=?", (report["id"],))
            path = f"/api/reports/{report['id']}/review-labels"
            self.assertEqual(409, client.get(path).status_code)
            self.assertEqual(409, client.put(path, json={"labels": []}).status_code)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],))
            claims = client.get(path).json()["claims"]
            saved = client.put(path, json={"labels": [
                {"index": item["index"], "label": "supported"} for item in claims
            ]})
            self.assertEqual(200, saved.status_code)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE review_claim_labels SET claims_sha256='changed' WHERE report_id=?", (
                    report["id"],
                ))
            self.assertTrue(client.get(path).json()["stale"])
            self.assertEqual(200, client.put(path, json={"labels": [
                {"index": item["index"], "label": "supported"} for item in claims
            ]}).status_code)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE reports SET source_json=? WHERE id=?", (
                    '{"kind":"daily","facts":["修改后的事实"],"projects":[]}', report["id"],
                ))
            self.assertTrue(client.get(path).json()["stale"])

    def test_manual_only_missed_span_is_counted_and_invalidated_with_final_text(self):
        final_text = "# 工作日报\n\n## 今日完成\n今天把上线复盘写入文档\n\n## 遇到的问题\n暂无\n"
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "missed.db")))
            store = client.app.state.store
            report = store.create_report("daily", "2026-10-04", {
                "kind": "daily", "facts": [], "projects": [],
            }, final_text, 1)
            store.confirm_report(report["id"], final_text)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],))
            path = f"/api/reports/{report['id']}/review-labels"
            case = client.get(path).json()
            self.assertEqual([], case["claims"])
            self.assertEqual("今天把上线复盘写入文档", case["completion_lines"][0]["text"])
            missed = {"line": 4, "column_start": 0, "column_end": len("今天把上线复盘写入文档")}
            self.assertEqual(409, client.put(path, json={"labels": [], "missed_claims": [
                {**missed, "line": 6},
            ]}).status_code)
            saved = client.put(path, json={"labels": [], "missed_claims": [missed]})
            self.assertEqual(200, saved.status_code, saved.text)
            self.assertTrue(saved.json()["labeled"])
            self.assertEqual("今天把上线复盘写入文档", saved.json()["missed_claims"][0]["text"])
            self.assertEqual(409, client.put(path, json={"labels": [], "missed_claims": [
                missed, missed,
            ]}).status_code)
            self.assertEqual(1, len(client.get(path).json()["missed_claims"]))
            result = client.get("/api/review-benchmark").json()
            self.assertEqual(1, result["labeled_claims"])
            self.assertEqual(1, result["counts"]["false_negative"])
            self.assertEqual("今天把上线复盘写入文档", result["mismatches"][0]["text"])
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE reports SET final_text=? WHERE id=?", (final_text + "\n", report["id"]))
            self.assertTrue(client.get(path).json()["stale"])
            self.assertEqual(0, client.get("/api/review-benchmark").json()["labeled_claims"])

    def test_nested_subclaim_uses_parent_as_single_scoring_unit(self):
        parent_text = "完成接口联调和异常路径覆盖顺带修订上线方案"
        final_text = f"# 工作日报\n\n## 今日完成\n- {parent_text}\n\n## 遇到的问题\n- 暂无\n"
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "nested.db")))
            store = client.app.state.store
            report = store.create_report("daily", "2026-10-05", {
                "kind": "daily", "facts": ["完成接口联调和异常路径覆盖"], "projects": [],
            }, final_text, 1)
            store.confirm_report(report["id"], final_text)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],))
            path = f"/api/reports/{report['id']}/review-labels"
            case = client.get(path).json()
            self.assertEqual(1, len(case["claims"]))
            self.assertFalse(case["claims"][0]["flagged"])
            line = final_text.splitlines()[3]
            child_text = "修订上线方案"
            start = line.index(child_text)
            child = {"line": 4, "column_start": start, "column_end": start + len(child_text)}
            self.assertEqual(409, client.put(path, json={"labels": [
                {"index": 0, "label": "supported"},
            ], "missed_claims": [child]}).status_code)
            saved = client.put(path, json={"labels": [{"index": 0, "label": "unsupported"}],
                                            "missed_claims": [child]})
            self.assertEqual(200, saved.status_code, saved.text)
            self.assertEqual(0, saved.json()["missed_claims"][0]["parent_claim_index"])
            result = client.get("/api/review-benchmark").json()
            self.assertEqual(1, result["labeled_claims"])
            self.assertEqual(1, result["nested_claims"])
            self.assertEqual(1, result["counts"]["false_negative"])
            self.assertEqual([child_text], result["mismatches"][0]["child_texts"])

            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE reports SET source_json=? WHERE id=?", (
                    '{"kind":"daily","facts":[],"projects":[]}', report["id"],
                ))
            self.assertTrue(client.get(path).json()["stale"])
            self.assertEqual(200, client.put(path, json={"labels": [{"index": 0, "label": "unsupported"}],
                                                           "missed_claims": [child]}).status_code)
            flagged_result = client.get("/api/review-benchmark").json()
            self.assertEqual(1, flagged_result["labeled_claims"])
            self.assertEqual(1, flagged_result["counts"]["true_positive"])
            self.assertEqual(0, flagged_result["counts"]["false_negative"])
            second_text = "异常路径覆盖"
            second_start = line.index(second_text)
            second = {"line": 4, "column_start": second_start,
                      "column_end": second_start + len(second_text)}
            self.assertEqual(200, client.put(path, json={"labels": [{"index": 0, "label": "unsupported"}],
                                                           "missed_claims": [child, second]}).status_code)
            grouped = client.get("/api/review-benchmark").json()
            self.assertEqual(2, grouped["nested_claims"])
            self.assertEqual(1, grouped["labeled_claims"])
            self.assertEqual(1, grouped["counts"]["true_positive"])

    def test_missed_span_cannot_cross_two_detected_claims(self):
        final_text = "# 工作日报\n\n## 今日完成\n- 完成接口联调；修复登录异常\n"
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "cross.db")))
            store = client.app.state.store
            report = store.create_report("daily", "2026-10-06", {
                "kind": "daily", "facts": [], "projects": [],
            }, final_text, 1)
            store.confirm_report(report["id"], final_text)
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],))
            path = f"/api/reports/{report['id']}/review-labels"
            claims = client.get(path).json()["claims"]
            self.assertEqual(2, len(claims))
            response = client.put(path, json={"labels": [
                {"index": claim["index"], "label": "unsupported"} for claim in claims
            ], "missed_claims": [{"line": 4, "column_start": claims[0]["column_start"] + 2,
                                  "column_end": claims[1]["column_end"]}]})
            self.assertEqual(409, response.status_code)
            self.assertEqual(0, client.get("/api/review-benchmark").json()["labeled_claims"])


if __name__ == "__main__":
    unittest.main()
