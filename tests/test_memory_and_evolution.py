import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from evoreport.api import create_app
from evoreport.config import Settings
from evoreport.evolution import EvolutionService
from evoreport.store import Store


SOURCE = {
    "kind": "daily", "date": "2026-10-14", "projects": [], "manual_completed": "完成接口联调",
    "manual_problems": "", "manual_tomorrow": "补充测试", "facts": ["完成接口联调"],
}
NUMBERED = "# 工作日报\n\n## 今日完成\n1. 完成接口联调\n\n## 遇到的问题\n1. 暂无\n\n## 明日计划\n1. 补充测试\n"
BULLETED = NUMBERED.replace("1. ", "- ")


class MemoryAndEvolutionTests(unittest.TestCase):
    def test_final_edits_propose_personal_preferences_without_holdout_learning(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "personal-suggestions.db")
            original = store.add_memory("personal", "numbered", "true")

            def add_feedback(day, split, final_text):
                initial = NUMBERED.replace("## 遇到的问题", "## 开发统计\n1. 提交 1 次\n\n## 遇到的问题")
                report = store.create_report("daily", day, SOURCE, initial, 1)
                store.confirm_report(report["id"], final_text)
                with closing(sqlite3.connect(store.path)) as connection, connection:
                    connection.execute("UPDATE evaluation_cases SET split=? WHERE report_id=?",
                                       (split, report["id"]))
                    connection.execute("UPDATE writing_feedback SET split=? WHERE report_id=?",
                                       (split, report["id"]))
                return report["id"]

            final = BULLETED
            first = add_feedback("2026-10-01", "validation", final)
            second = add_feedback("2026-10-02", "validation", final)
            add_feedback("2026-10-03", "holdout", NUMBERED)
            suggestions = store.list_memory_suggestions()
            numbered = next(item for item in suggestions
                            if item["scope"] == "personal" and item["memory_key"] == "numbered")
            statistics = next(item for item in suggestions
                              if item["scope"] == "personal" and item["memory_key"] == "include_statistics")
            self.assertEqual("false", numbered["value"])
            self.assertEqual({first, second}, {item["report_id"] for item in numbered["evidence"]})
            self.assertEqual([original["id"]], [item["id"] for item in numbered["conflicts_with"]])
            self.assertEqual("false", statistics["value"])
            accepted = store.decide_memory_suggestion(numbered["id"], "accept")
            self.assertEqual("accepted", accepted["status"])
            self.assertEqual(original["id"], accepted["memory"]["supersedes_id"])
            self.assertFalse(store.memory_context([])["preferences"]["numbered"])

    def test_similar_project_constraints_merge_and_conflicts_are_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "semantic-memory.db")
            project = store.add_project("项目", str(Path(directory) / "project"), "plain")
            existing = store.add_memory("project", "note", "接口不再需要支持旧版本", project["id"])
            another_conflict = store.add_memory("project", "note", "API不再需要兼容旧版", project["id"])
            for day, phrase in (("2026-10-01", "接口必须兼容旧版本"),
                                ("2026-10-02", "API需要支持旧版")):
                source = {"kind": "daily", "projects": [{"id": project["id"]}],
                          "manual_completed": "", "manual_problems": phrase,
                          "manual_tomorrow": "", "facts": []}
                report = store.create_report("daily", day, source, "# 草稿", 1)
                store.confirm_report(report["id"], "# 终稿")
                with closing(sqlite3.connect(store.path)) as connection, connection:
                    connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?",
                                       (report["id"],))
            candidate = next(item for item in store.list_memory_suggestions() if item["scope"] == "project")
            self.assertEqual(2, len(candidate["evidence"]))
            self.assertEqual({existing["id"], another_conflict["id"]},
                             {item["id"] for item in candidate["conflicts_with"]})
            accepted = store.decide_memory_suggestion(candidate["id"], "accept")
            self.assertIn(accepted["memory"]["supersedes_id"], {existing["id"], another_conflict["id"]})
            self.assertEqual("corrected", store.get_memory(existing["id"])["status"])
            self.assertEqual("corrected", store.get_memory(another_conflict["id"])["status"])

            other = store.add_project("另一项目", str(Path(directory) / "other"), "plain")
            merge_target = store.add_memory("project", "note", "接口需要支持旧版本协议", other["id"])
            for day, phrase in (("2026-10-03", "接口必须兼容旧版本"),
                                ("2026-10-04", "API需要支持旧版")):
                source = {"kind": "daily", "projects": [{"id": other["id"]}],
                          "manual_completed": "", "manual_problems": phrase,
                          "manual_tomorrow": "", "facts": []}
                report = store.create_report("daily", day, source, "# 草稿", 1)
                store.confirm_report(report["id"], "# 终稿")
                with closing(sqlite3.connect(store.path)) as connection, connection:
                    connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?",
                                       (report["id"],))
            merged = next(item for item in store.list_memory_suggestions()
                          if item["scope"] == "project" and item["project_id"] == other["id"])
            self.assertEqual([merge_target["id"]], [item["id"] for item in merged["merge_with"]])

    def test_project_memory_guides_sections_and_style_without_becoming_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "project").mkdir()
            client = TestClient(create_app(Settings("127.0.0.1", 8090, root / "guidance.db")))
            project = client.post("/api/projects", json={
                "name": "项目", "path": str(root / "project"), "project_type": "plain",
            }).json()
            note_text = "长期关注兼容风险，日报使用项目符号并保持简洁"
            note = client.post("/api/memories", json={
                "scope": "project", "memory_key": "note", "project_id": project["id"],
                "value": note_text,
            }).json()
            report = client.post("/api/reports/daily", json={
                "report_date": "2026-10-17", "project_ids": [project["id"]],
                "completed": "完成接口联调并补充覆盖多个边界条件的自动化回归测试与部署验证记录",
            }).json()
            guidance = report["source"]["memory_context"]["project_guidance"]
            self.assertFalse(guidance["expression_preferences"]["numbered"])
            self.assertEqual(70, guidance["expression_preferences"]["max_item_length"])
            self.assertEqual(["遇到的问题", "明日计划"], guidance["section_focus"][0]["sections"])
            self.assertEqual([note["id"]], guidance["section_focus"][0]["source_ids"])
            self.assertIn("- 完成接口联调", report["draft"])
            self.assertNotIn(note_text, report["source"]["facts"])
            self.assertNotIn(note_text, str(report["agent_trace"]))
            planner = report["agent_trace"]["steps"][0]
            self.assertEqual(1, len(planner["output"]["focused_sections"]))
            self.assertEqual(2, planner["output"]["task_actions"].count("preserve_section"))

    def test_memory_suggestions_require_repeated_validation_and_user_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = TestClient(create_app(Settings("127.0.0.1", 8090, root / "suggestions.db")))
            store = client.app.state.store
            project = store.add_project("甲项目", str(root / "one"), "plain")
            other = store.add_project("乙项目", str(root / "two"), "plain")

            def add_case(day, project_ids, phrase, split):
                source = {
                    "kind": "daily", "date": day,
                    "projects": [{"id": item} for item in project_ids],
                    "manual_completed": "", "manual_problems": phrase,
                    "manual_tomorrow": "", "facts": [],
                }
                report = store.create_report("daily", day, source, "# 初稿", 1)
                store.confirm_report(report["id"], "# 终稿")
                with closing(sqlite3.connect(store.path)) as connection, connection:
                    connection.execute("UPDATE evaluation_cases SET split=? WHERE report_id=?", (split, report["id"]))
                return report["id"]

            phrase = "接口必须兼容旧版本"
            first = add_case("2026-10-01", [project["id"]], phrase, "validation")
            add_case("2026-10-02", [project["id"]], phrase, "holdout")
            self.assertEqual([], client.get("/api/memories/suggestions").json()["suggestions"])
            second = add_case("2026-10-03", [project["id"]], phrase, "validation")
            add_case("2026-10-04", [project["id"], other["id"]], "部署必须经过审核", "validation")
            add_case("2026-10-05", [project["id"]], "部署必须经过审核", "validation")
            add_case("2026-10-06", [other["id"]], phrase, "validation")
            suggestions = client.get("/api/memories/suggestions").json()["suggestions"]
            self.assertEqual(1, len(suggestions))
            candidate = suggestions[0]
            self.assertEqual(phrase, candidate["value"])
            self.assertEqual({first, second}, {entry["report_id"] for entry in candidate["evidence"]})
            self.assertEqual({"2026-10-01", "2026-10-03"},
                             {entry["report_date"] for entry in candidate["evidence"]})
            self.assertTrue(all(entry["field"] == "manual_problems" and entry["line"] == 1
                                for entry in candidate["evidence"]))
            self.assertEqual([], store.memory_context([project["id"]])["project_notes"])

            with patch("evoreport.store._memory_today", return_value=date(2026, 10, 16)):
                accepted = client.post(f"/api/memories/suggestions/{candidate['id']}/decision", json={
                    "action": "accept", "expires_on": "2026-10-20",
                })
                self.assertEqual(200, accepted.status_code, accepted.text)
                memory = accepted.json()["memory"]
                self.assertEqual("archive_candidate", memory["source_type"])
                self.assertEqual(candidate["id"], memory["source_id"])
                self.assertEqual("2026-10-20", memory["expires_on"])
                self.assertEqual(1, len(store.memory_context([project["id"]])["project_notes"]))
            self.assertEqual([], client.get("/api/memories/suggestions").json()["suggestions"])
            self.assertEqual(409, client.post(f"/api/memories/suggestions/{candidate['id']}/decision",
                                              json={"action": "accept"}).status_code)
            with closing(sqlite3.connect(store.path)) as connection:
                saved = connection.execute("SELECT status,evidence_json FROM memory_suggestion_decisions").fetchone()
            self.assertEqual("accepted", saved[0])
            self.assertEqual({first, second}, {entry["report_id"] for entry in json.loads(saved[1])})

    def test_ignored_memory_suggestion_stays_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "ignored.db")
            project = store.add_project("项目", str(Path(directory) / "project"), "plain")
            for day in ("2026-10-01", "2026-10-02"):
                source = {"kind": "daily", "projects": [{"id": project["id"]}],
                          "manual_completed": "", "manual_problems": "数据库必须保留审计日志",
                          "manual_tomorrow": "", "facts": []}
                report = store.create_report("daily", day, source, "# 初稿", 1)
                store.confirm_report(report["id"], "# 终稿")
                with closing(sqlite3.connect(store.path)) as connection, connection:
                    connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],))
            candidate = store.list_memory_suggestions()[0]
            self.assertEqual("ignored", store.decide_memory_suggestion(candidate["id"], "ignore")["status"])
            self.assertEqual([], store.list_memory_suggestions())
            self.assertEqual([], store.list_memories())
            self.assertEqual([], Store(store.path).list_memory_suggestions())

    def test_project_note_expires_without_changing_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "project").mkdir()
            client = TestClient(create_app(Settings("127.0.0.1", 8090, root / "memory-expiry.db")))
            project = client.post("/api/projects", json={
                "name": "项目", "path": str(root / "project"), "project_type": "plain",
            }).json()
            with patch("evoreport.store._memory_today", return_value=date(2026, 10, 16)):
                response = client.post("/api/memories", json={
                    "scope": "project", "memory_key": "note", "project_id": project["id"],
                    "value": "长期关注兼容性", "expires_on": "2026-10-16",
                })
                self.assertEqual(201, response.status_code, response.text)
                note = response.json()
                self.assertFalse(note["is_expired"])
                first = client.post("/api/reports/daily", json={
                    "report_date": "2026-10-16", "project_ids": [project["id"]],
                    "completed": "完成接口联调",
                }).json()
                self.assertEqual([note["id"]], [item["id"] for item in
                                 first["source"]["memory_context"]["project_notes"]])
            with patch("evoreport.store._memory_today", return_value=date(2026, 10, 17)):
                self.assertEqual([], client.get("/api/memories").json()["memories"])
                shown = client.get("/api/memories?include_expired=true").json()["memories"]
                self.assertEqual([note["id"]], [item["id"] for item in shown])
                self.assertTrue(shown[0]["is_expired"])
                self.assertEqual([], client.app.state.store.memory_context([project["id"]])["project_notes"])
                second = client.post("/api/reports/daily", json={
                    "report_date": "2026-10-17", "project_ids": [project["id"]],
                    "completed": "补充接口测试",
                }).json()
                self.assertEqual([], second["source"]["memory_context"]["project_notes"])
                saved_first = client.get(f"/api/reports/{first['id']}").json()
                self.assertEqual(note["id"], saved_first["source"]["memory_context"]["project_notes"][0]["id"])
                renewed = client.patch(f"/api/memories/{note['id']}", json={
                    "value": "持续关注兼容性", "expires_on": "2026-10-20",
                })
                self.assertEqual(200, renewed.status_code, renewed.text)
                self.assertEqual("2026-10-20", renewed.json()["expires_on"])
                self.assertEqual(note["id"], renewed.json()["supersedes_id"])
                preserved = client.patch(f"/api/memories/{renewed.json()['id']}", json={
                    "value": "继续关注接口兼容性",
                })
                self.assertEqual(200, preserved.status_code, preserved.text)
                self.assertEqual("2026-10-20", preserved.json()["expires_on"])
                ongoing = client.patch(f"/api/memories/{preserved.json()['id']}", json={
                    "value": "长期关注接口兼容性", "expires_on": None,
                })
                self.assertEqual(200, ongoing.status_code, ongoing.text)
                self.assertIsNone(ongoing.json()["expires_on"])
                self.assertEqual(1, len(client.app.state.store.memory_context([project["id"]])["project_notes"]))

    def test_memory_expiry_validation_and_legacy_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old-memory.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("""CREATE TABLE memory_entries (
                    id TEXT PRIMARY KEY,scope TEXT NOT NULL,project_id TEXT,memory_key TEXT NOT NULL,
                    value TEXT NOT NULL,source_type TEXT NOT NULL,source_id TEXT,supersedes_id TEXT,
                    status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                )""")
            store = Store(path)
            with closing(sqlite3.connect(path)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_entries)")}
            self.assertIn("expires_on", columns)
            project = store.add_project("项目", str(Path(directory) / "project"), "plain")
            with patch("evoreport.store._memory_today", return_value=date(2026, 10, 16)):
                with self.assertRaisesRegex(ValueError, "不能早于今天"):
                    store.add_memory("project", "note", "背景", project["id"], expires_on="2026-10-15")
                with self.assertRaisesRegex(ValueError, "到期日期无效"):
                    store.add_memory("project", "note", "背景", project["id"], expires_on="not-a-date")
                with self.assertRaisesRegex(ValueError, "个人写作偏好不支持"):
                    store.add_memory("personal", "numbered", "true", expires_on="2026-10-20")
                old = store.add_memory("project", "note", "背景", project["id"])
                self.assertIsNone(old["expires_on"])
                corrected = store.correct_memory(old["id"], "修正背景")
                self.assertIsNone(corrected["expires_on"])

    def test_existing_evaluation_table_adds_evidence_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("""CREATE TABLE evaluation_runs (
                    id TEXT PRIMARY KEY,candidate_version INTEGER NOT NULL,decision TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL
                )""")
            store = Store(path)
            run_id = store.save_evaluation_run(1, "deferred", {}, "测试迁移", {"rules": []})
            self.assertEqual(run_id, store.list_evaluation_runs()[0]["id"])
            self.assertEqual({"rules": []}, store.list_evaluation_runs()[0]["evidence"])

    def test_memory_api_correction_cleanup_and_report_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one").mkdir()
            (root / "two").mkdir()
            client = TestClient(create_app(Settings("127.0.0.1", 8090, root / "memory.db")))
            first = client.post("/api/projects", json={"name": "甲项目", "path": str(root / "one"), "project_type": "plain"}).json()
            second = client.post("/api/projects", json={"name": "乙项目", "path": str(root / "two"), "project_type": "plain"}).json()
            preference = client.post("/api/memories", json={
                "scope": "personal", "memory_key": "numbered", "value": "false",
            })
            self.assertEqual(201, preference.status_code)
            note = client.post("/api/memories", json={
                "scope": "project", "memory_key": "note", "value": "长期维护接口兼容性 token=example-value", "project_id": second["id"],
            })
            self.assertEqual(201, note.status_code)
            self.assertNotIn("example-value", note.json()["value"])
            self.assertEqual(1, len(client.app.state.store.memory_context([second["id"]])["project_notes"]))
            report = client.post("/api/reports/daily", json={
                "report_date": "2026-10-14", "project_ids": [first["id"]], "completed": "完成接口联调",
            }).json()
            self.assertIn("- 完成接口联调", report["draft"])
            self.assertEqual([], report["source"]["memory_context"]["project_notes"])
            context = client.get(f"/api/reports/{report['id']}/memory-context").json()
            self.assertEqual(["完成接口联调"], context["task_facts"])
            self.assertFalse(context["memory"]["preferences"]["numbered"])

            correction = client.patch(f"/api/memories/{preference.json()['id']}", json={"value": "true"})
            self.assertEqual(200, correction.status_code)
            self.assertEqual(preference.json()["id"], correction.json()["supersedes_id"])
            self.assertEqual("corrected", client.app.state.store.get_memory(preference.json()["id"])["status"])
            self.assertTrue(client.app.state.store.memory_context([])["preferences"]["numbered"])
            self.assertEqual(200, client.delete(f"/api/memories/{note.json()['id']}").status_code)
            self.assertEqual([], client.app.state.store.memory_context([second["id"]])["project_notes"])
            self.assertIn("长期维护接口兼容性", client.app.state.store.get_memory(note.json()["id"])["value"])

    def test_validation_feedback_and_critic_evidence_exclude_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evolution.db"
            store = Store(path)
            reports = []
            for _ in range(4):
                report = store.create_report("daily", "2026-10-14", SOURCE, NUMBERED, 1)
                store.confirm_report(report["id"], BULLETED)
                reports.append(report)
            with closing(sqlite3.connect(path)) as connection, connection:
                for report in reports[:3]:
                    connection.execute("UPDATE evaluation_cases SET split='validation' WHERE report_id=?", (report["id"],))
                    connection.execute("UPDATE writing_feedback SET split='validation' WHERE report_id=?", (report["id"],))
                connection.execute("UPDATE evaluation_cases SET split='holdout' WHERE report_id=?", (reports[3]["id"],))
                connection.execute("UPDATE writing_feedback SET split='holdout' WHERE report_id=?", (reports[3]["id"],))
            validation_run = store.start_agent_run("daily_report", reports[0]["id"])
            store.add_agent_step(validation_run, "Critic Agent", "needs_review", {}, {"missing_fact_indexes": [0]}, {}, 0)
            holdout_run = store.start_agent_run("daily_report", reports[3]["id"])
            store.add_agent_step(holdout_run, "Critic Agent", "needs_review", {}, {"missing_fact_indexes": [0]}, {}, 0)

            result = EvolutionService(store).propose()
            evidence = result["evidence"]
            self.assertEqual({item["id"] for item in reports[:3]}, set(evidence["feedback_report_ids"]))
            self.assertNotIn(reports[3]["id"], evidence["validation_report_ids"])
            self.assertEqual([validation_run], evidence["critic_run_ids"])
            self.assertNotIn(holdout_run, str(evidence))
            self.assertFalse(result["candidate"]["config"]["numbered"])
            self.assertGreaterEqual(result["candidate"]["config"]["max_item_length"], 120)
            saved = store.list_evaluation_runs()[0]
            self.assertEqual(evidence, saved["evidence"])

    def test_activated_learning_record_does_not_override_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollback.db"
            store = Store(path)
            for index in range(4):
                report = store.create_report("daily", "2026-10-14", SOURCE, NUMBERED, 1)
                store.confirm_report(report["id"], BULLETED)
                split = "validation" if index < 3 else "holdout"
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute("UPDATE evaluation_cases SET split=? WHERE report_id=?", (split, report["id"]))
                    connection.execute("UPDATE writing_feedback SET split=? WHERE report_id=?", (split, report["id"]))
            result = EvolutionService(store).propose()
            self.assertEqual("activated", result["decision"])
            learned = [item for item in store.list_memories() if item["source_type"] == "evaluation"]
            self.assertTrue(learned)
            self.assertTrue(all(item["source_id"] == result["evaluation_run_id"] for item in learned))
            self.assertTrue(store.activate_strategy(1))
            self.assertEqual(1, store.active_strategy()["version"])
            self.assertEqual({}, store.memory_context([])["preferences"])


if __name__ == "__main__":
    unittest.main()
