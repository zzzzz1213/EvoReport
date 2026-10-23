import sqlite3
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from evoreport.api import create_app
from evoreport.agent_runtime import MultiAgentRuntime
from evoreport.agent_instructions import CRITIC_RULE_VERSION
from evoreport.role_executor import ROLE_PROTOCOL_VERSION
from evoreport.config import Settings
from evoreport.llm import ArkReportWriter
from evoreport.evaluation import review_claims, review_report
from evoreport.store import Store


class FinalReviewTests(unittest.TestCase):
    def test_structured_semantic_match_explains_supported_uncertain_and_unsupported(self):
        source = {"kind": "daily", "facts": ["实现API鉴权"]}
        candidate = "## 今日完成\n- 完成接口鉴权\n## 遇到的问题\n## 明日计划\n"
        claim = review_claims(candidate)[0]
        self.assertEqual("完成", claim["structure"]["action"])
        self.assertEqual("delivery", claim["structure"]["action_family"])
        self.assertEqual("接口鉴权", claim["structure"]["object"])
        self.assertEqual([], review_report(source, candidate, 65, 0, 60, 10)["unsupported_claims"])

        uncertain = review_report(source, candidate, 70, 0, 60, 10)["unsupported_claims"][0]
        self.assertEqual("uncertain", uncertain["support_status"])
        self.assertGreater(uncertain["score_components"]["semantic"], 0.9)
        pending = review_report(
            source, candidate.replace("完成接口鉴权", "计划完成接口鉴权"), 65, 0, 60, 10,
        )["unsupported_claims"][0]
        self.assertEqual("unsupported", pending["support_status"])
        self.assertEqual("pending", pending["structure"]["polarity"])

    def test_confirmed_final_review_replays_without_returning_body(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "test.db")))
            report = client.post("/api/reports/daily", json={
                "report_date": "2026-10-13", "completed": "完成接口联调",
            }).json()
            confirmed = client.post(f"/api/reports/{report['id']}/confirm", json={
                "final_text": report["draft"],
            })
            self.assertEqual(200, confirmed.status_code, confirmed.text)
            run_id = confirmed.json()["final_review"]["run_id"]
            replay = client.get(f"/api/agents/{run_id}/replay")
            self.assertEqual(200, replay.status_code)
            self.assertEqual("verified", replay.json()["status"])
            self.assertEqual(2, len(replay.json()["steps"]))
            self.assertNotIn("完成接口联调", replay.text)

    def test_acknowledged_warning_can_be_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            source = {"kind": "daily", "facts": ["完成接口联调"]}
            report = store.create_report("daily", "2026-10-13", source, "draft", 1)
            final_text = "## 今日完成\n## 遇到的问题\n## 明日计划\n"
            runtime = MultiAgentRuntime(store, ArkReportWriter("", "", ""))
            review = runtime.review_final(report["id"], source, final_text, True)
            self.assertEqual("passed", review["decision"])
            store.confirm_report(report["id"], final_text)
            self.assertEqual("verified", runtime.replay_report(review["run_id"])["status"])

    def test_final_replay_detects_changed_input_and_step_result(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            source = {"kind": "daily", "facts": ["完成接口联调"]}
            final_text = "## 今日完成\n- 完成接口联调\n## 遇到的问题\n## 明日计划\n"
            report = store.create_report("daily", "2026-10-13", source, "draft", 1)
            runtime = MultiAgentRuntime(store, ArkReportWriter("", "", ""))
            run_id = runtime.review_final(report["id"], source, final_text)["run_id"]
            store.confirm_report(report["id"], final_text)
            steps = store.get_agent_run(run_id)["steps"]
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE reports SET final_text=? WHERE id=?", (final_text + "改动", report["id"]))
            self.assertEqual("终稿与审查输入不一致", runtime.replay_report(run_id)["steps"][0]["reason"])
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE reports SET final_text=?,source_json=? WHERE id=?",
                                   (final_text.strip(), json.dumps({"kind": "daily", "facts": []}), report["id"]))
            self.assertEqual("来源快照已变化", runtime.replay_report(run_id)["steps"][0]["reason"])
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE reports SET source_json=? WHERE id=?",
                                   (json.dumps(source), report["id"]))
                connection.execute("UPDATE agent_steps SET output_json='{}' WHERE id=?", (steps[0]["id"],))
            self.assertEqual("Critic 审查结果不一致", runtime.replay_report(run_id)["steps"][0]["reason"])
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE agent_steps SET output_json=? WHERE id=?",
                                   (json.dumps(steps[0]["output"], ensure_ascii=False), steps[0]["id"]))
                connection.execute("UPDATE agent_steps SET status='needs_review' WHERE id=?", (steps[1]["id"],))
            self.assertEqual("Gate 决策不一致", runtime.replay_report(run_id)["steps"][1]["reason"])

    def test_old_or_unconfirmed_final_review_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            source = {"kind": "daily", "facts": ["完成接口联调"]}
            final_text = "## 今日完成\n- 完成接口联调\n## 遇到的问题\n## 明日计划\n"
            report = store.create_report("daily", "2026-10-13", source, "draft", 1)
            runtime = MultiAgentRuntime(store, ArkReportWriter("", "", ""))
            run_id = runtime.review_final(report["id"], source, final_text)["run_id"]
            self.assertEqual("unavailable", runtime.replay_report(run_id)["status"])
            store.confirm_report(report["id"], final_text)
            steps = store.get_agent_run(run_id)["steps"]
            critic_id = steps[0]["id"]
            old_output = dict(steps[0]["output"])
            old_output["execution"] = dict(old_output["execution"])
            old_output["execution"]["protocol_version"] = ROLE_PROTOCOL_VERSION - 1
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE agent_steps SET output_json=? WHERE id=?",
                                   (json.dumps(old_output, ensure_ascii=False), critic_id))
            protocol_replay = runtime.replay_report(run_id)
            self.assertEqual("unavailable", protocol_replay["status"])
            self.assertIn("协议版本", protocol_replay["reason"])
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE agent_steps SET output_json=? WHERE id=?",
                                   (json.dumps(steps[0]["output"], ensure_ascii=False), critic_id))
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE agent_runs SET instruction_json=? WHERE id=?",
                                   (json.dumps({"critic_rules": {"version": -1}}), run_id))
            self.assertIn("版本不兼容", runtime.replay_report(run_id)["reason"])
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("UPDATE agent_runs SET instruction_json=? WHERE id=?",
                                   (json.dumps({"critic_rules": {"version": CRITIC_RULE_VERSION}}), run_id))
                connection.execute("UPDATE agent_steps SET input_json='{}' WHERE id=?", (critic_id,))
            replay = runtime.replay_report(run_id)
            self.assertEqual("unavailable", replay["status"])
            self.assertIn("旧运行", replay["reason"])

    def test_edited_final_uses_generated_snapshot_for_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "test.db")))
            report = client.post("/api/reports/daily", json={
                "report_date": "2026-10-13", "completed": "完成联调",
            }).json()
            edited = report["draft"].replace("完成联调", "完成联调并补充回归")
            saved = client.patch(f"/api/reports/{report['id']}/draft", json={"draft": edited})
            self.assertEqual(200, saved.status_code)
            self.assertEqual(report["draft"], saved.json()["initial_draft"])

            confirmed = client.post(f"/api/reports/{report['id']}/confirm", json={"final_text": edited})
            self.assertEqual(200, confirmed.status_code, confirmed.text)
            store = client.app.state.store
            case = store.list_evaluation_cases()[0]
            feedback = store.list_writing_feedback(case["split"])[0]
            self.assertEqual(report["id"], feedback["report_id"])
            self.assertTrue(feedback["signals"]["changed"])
            self.assertGreater(feedback["signals"]["character_delta"], 0)
            self.assertNotIn("完成联调", str(feedback["signals"]))
            self.assertEqual("final_review", store.list_agent_runs(1)[0]["workflow"])
            self.assertEqual(409, client.post(
                f"/api/reports/{report['id']}/confirm", json={"final_text": edited}
            ).status_code)
            self.assertEqual(1, len(store.list_evaluation_cases()))

    def test_sensitive_final_is_blocked_even_with_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "test.db")))
            report = client.post("/api/reports/daily", json={
                "report_date": "2026-10-13", "completed": "完成联调",
            }).json()
            final_text = report["draft"] + "\ntoken=example-value"
            response = client.post(f"/api/reports/{report['id']}/confirm", json={
                "final_text": final_text,
                "acknowledge_warnings": True,
            })
            self.assertEqual(409, response.status_code)
            self.assertTrue(response.json()["detail"]["review"]["unsafe"])
            self.assertEqual(final_text.splitlines().index("token=example-value") + 1, response.json()["detail"]["review"]["issues"][0]["line"])
            store = client.app.state.store
            self.assertEqual("draft", store.get_report(report["id"])["status"])
            self.assertEqual([], store.list_evaluation_cases())
            run = store.list_agent_runs(1)[0]
            self.assertEqual("blocked", run["status"])
            self.assertNotIn("example-value", str(run))

    def test_one_missing_fact_still_requires_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            facts = [f"事项{label}完成" for label in "甲乙丙丁戊"]
            report = store.create_report("daily", "2026-10-13", {"kind": "daily", "facts": facts}, "draft", 1)
            final_text = "## 今日完成\n" + "\n".join(f"- {fact}" for fact in facts[:4]) + "\n## 遇到的问题\n## 明日计划\n"
            runtime = MultiAgentRuntime(store, ArkReportWriter("", "", ""))
            result = runtime.review_final(report["id"], report["source"], final_text)
            self.assertEqual("needs_review", result["decision"])
            self.assertEqual([4], result["review"]["missing_fact_indexes"])

    def test_review_reports_source_evidence_and_unsupported_line(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "test.db")))
            report = client.post("/api/reports/daily", json={
                "report_date": "2026-10-13", "completed": "完成接口联调",
            }).json()
            self.assertEqual({"source_type": "manual", "line": 1}, report["source"]["fact_sources"][0])
            final_text = report["draft"].replace(
                "## 遇到的问题", "- 完成未经记录的生产发布\n\n## 遇到的问题"
            )
            response = client.post(f"/api/reports/{report['id']}/confirm", json={"final_text": final_text})
            self.assertEqual(409, response.status_code)
            issues = response.json()["detail"]["review"]["issues"]
            claim = next(item for item in issues if item["type"] == "unsupported_claim")
            self.assertEqual(final_text.splitlines().index("- 完成未经记录的生产发布") + 1, claim["line"])
            self.assertLess(claim["match_score"], 0.5)
            self.assertIn("完成接口联调", claim["evidence"])
            self.assertIn("核对", claim["suggestion"])
            self.assertEqual("draft", client.app.state.store.get_report(report["id"])["status"])
            self.assertEqual(200, client.post(f"/api/reports/{report['id']}/confirm", json={
                "final_text": final_text, "acknowledge_warnings": True,
            }).status_code)
            run = client.app.state.store.list_agent_runs(1)[0]
            self.assertNotIn("完成未经记录的生产发布", str(run))

    def test_missing_fact_uses_old_snapshot_fallback_and_redacts_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "test.db")))
            store = client.app.state.store
            report = store.create_report("daily", "2026-10-13", {
                "kind": "daily", "facts": ["核对 token=sample-private-value"],
            }, "draft", 1)
            response = client.post(f"/api/reports/{report['id']}/confirm", json={
                "final_text": "## 今日完成\n## 遇到的问题\n## 明日计划\n",
            })
            self.assertEqual(409, response.status_code)
            issue = response.json()["detail"]["review"]["issues"][0]
            self.assertIn("来源快照事实 1", issue["evidence"])
            self.assertNotIn("sample-private-value", response.text)

    def test_placeholder_and_plans_are_not_unsupported_claims(self):
        source = {"kind": "daily", "facts": []}
        candidate = "## 今日完成\n- 暂无已记录内容\n## 遇到的问题\n- 新增未知问题；补充讨论\n## 明日计划\n- 完成上线发布；完成回归测试\n"
        self.assertEqual([], review_report(source, candidate)["unsupported_claims"])

    def test_composite_completion_checks_each_explicit_segment(self):
        source = {"kind": "daily", "facts": ["完成接口联调"]}
        candidate = "## 今日完成\n- 完成接口联调；完成未经记录的生产发布\n## 遇到的问题\n## 明日计划\n"
        review = review_report(source, candidate)
        self.assertEqual([], review["missing_fact_indexes"])
        self.assertEqual(1, len(review["unsupported_claims"]))
        claim = review["unsupported_claims"][0]
        self.assertEqual(2, claim["line"])
        self.assertEqual("完成未经记录的生产发布", candidate.splitlines()[1][claim["column_start"]:claim["column_end"]])
        self.assertLess(claim["match_score"], 0.5)
        self.assertFalse(review["passed"])
        self.assertNotIn("完成未经记录的生产发布", str(review))

        supported = {"kind": "daily", "facts": ["完成接口联调", "完成回归测试"]}
        candidate = "## 今日完成\n- 完成接口联调，并且完成回归测试\n## 遇到的问题\n## 明日计划\n"
        self.assertEqual([], review_report(supported, candidate)["unsupported_claims"])

    def test_semantic_candidate_uses_fact_segments_and_bounded_aliases(self):
        source = {"kind": "daily", "facts": [
            "扩充认证模块端到端测试，覆盖登录成功、登录失败等场景；原文关联 PR #1069。",
        ]}
        candidate = (
            "## 今日完成\n"
            "- 完善认证模块的 E2E 测试，覆盖正常登录和失败尝试。\n"
            "## 遇到的问题\n## 明日计划\n"
        )
        semantic_review = review_report(source, candidate, 60, 0, 60, 0)
        self.assertEqual([], semantic_review["unsupported_claims"])

        legacy_review = review_report(source, candidate)
        self.assertTrue(legacy_review["unsupported_claims"])

        unsupported = candidate.replace(
            "完善认证模块的 E2E 测试，覆盖正常登录和失败尝试",
            "完成患者注册模块的端到端测试",
        )
        self.assertEqual(1, len(review_report(source, unsupported, 60, 0, 60, 0)["unsupported_claims"]))

    def test_semantic_candidate_keeps_descriptive_continuation_with_claim(self):
        source = {"kind": "daily", "facts": [
            "改进 Worklog 汇总页面的项目概览、项目选择、时间线和项目待办；"
            "页面列有本地提交及生成日报、周报的证据。",
        ]}
        candidate = (
            "## 今日完成\n"
            "- 改进 Worklog 汇总页面的项目概览、项目选择、时间线和项目待办；"
            "页面列有本地提交及生成日报、周报的证据。\n"
            "## 遇到的问题\n## 明日计划\n"
        )
        claims = review_claims(candidate, semantic_mode=True)
        self.assertEqual(1, len(claims))
        self.assertEqual([], review_report(source, candidate, 60, 0, 40, 0)["unsupported_claims"])

        explicit = candidate.replace(
            "页面列有本地提交及生成日报、周报的证据",
            "完成未经记录的生产发布",
        )
        self.assertEqual(1, len(review_report(source, explicit, 60, 0, 40, 0)["unsupported_claims"]))

    def test_composite_actions_and_near_matching_claims_need_review(self):
        source = {"kind": "daily", "facts": ["完成接口联调"]}
        for text, expected in (
            ("完成接口联调，修复登录问题", "修复登录问题"),
            ("完成接口联调、上线生产环境", "上线生产环境"),
            ("完成接口联调并完成回归测试", "完成回归测试"),
            ("完成接口上线", "完成接口上线"),
            ("未完成接口联调", "未完成接口联调"),
        ):
            with self.subTest(text=text):
                candidate = f"## 今日完成\n- {text}\n## 遇到的问题\n## 明日计划\n"
                review = review_report(source, candidate)
                self.assertEqual(1, len(review["unsupported_claims"]))
                claim = review["unsupported_claims"][0]
                self.assertEqual(expected, candidate.splitlines()[1][claim["column_start"]:claim["column_end"]])
                self.assertFalse(review["passed"])

        supported = {"kind": "daily", "facts": ["完成接口联调", "修复登录问题"]}
        candidate = "## 今日完成\n- 已完成接口联调，并修复登录问题\n## 遇到的问题\n## 明日计划\n"
        self.assertEqual([], review_report(supported, candidate)["unsupported_claims"])
        project_source = {"kind": "daily", "facts": ["完成接口联调"]}
        project_text = "## 今日完成\n- 示例项目：完成接口联调\n## 遇到的问题\n## 明日计划\n"
        self.assertEqual([], review_report(project_source, project_text)["unsupported_claims"])

    def test_short_explicit_action_and_source_location(self):
        source = {"kind": "daily", "facts": []}
        candidate = "## 今日完成\n- 已上线\n## 遇到的问题\n## 明日计划\n"
        self.assertEqual(1, len(review_report(source, candidate)["unsupported_claims"]))

        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "test.db")))
            report = client.post("/api/reports/daily", json={
                "report_date": "2026-10-13", "completed": "完成接口联调",
            }).json()
            final_text = report["draft"].replace("完成接口联调", "完成接口上线", 1)
            response = client.post(f"/api/reports/{report['id']}/confirm", json={"final_text": final_text})
            self.assertEqual(409, response.status_code)
            claim = next(item for item in response.json()["detail"]["review"]["issues"]
                         if item["type"] == "unsupported_claim")
            self.assertIn("手工记录第 1 行", claim["evidence"])
            self.assertIn("完成接口联调", claim["evidence"])
            self.assertIn("待核实", claim["summary"])
            self.assertLess(claim["match_score"], 0.6)

            git_report = client.app.state.store.create_report("daily", "2026-10-14", {
                "kind": "daily", "facts": ["完成接口联调"],
                "fact_sources": [{"source_type": "git_commit", "project_name": "示例项目", "commit_hash": "abcdef1234567890"}],
            }, "draft", 1)
            git_response = client.post(f"/api/reports/{git_report['id']}/confirm", json={
                "final_text": "## 今日完成\n- 完成接口上线\n## 遇到的问题\n## 明日计划\n",
            })
            self.assertEqual(409, git_response.status_code)
            git_claim = next(item for item in git_response.json()["detail"]["review"]["issues"]
                             if item["type"] == "unsupported_claim")
            self.assertIn("示例项目 · 提交 abcdef123456", git_claim["evidence"])

            private_source = {"kind": "daily", "facts": ["完成接口联调"],
                              "fact_sources": [{"source_type": "git_commit",
                                                "project_name": "示例项目 token=sample-private-value",
                                                "commit_hash": "abcdef1234567890"}]}
            private_report = client.app.state.store.create_report(
                "daily", "2026-10-15", private_source, "draft", 1,
            )
            private_response = client.post(f"/api/reports/{private_report['id']}/confirm", json={
                "final_text": "## 今日完成\n- 完成接口上线\n## 遇到的问题\n## 明日计划\n",
            })
            self.assertEqual(409, private_response.status_code)
            self.assertNotIn("sample-private-value", private_response.text)

    def test_composite_review_api_identifies_and_redacts_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(Settings("127.0.0.1", 8090, Path(directory) / "test.db")))
            report = client.post("/api/reports/daily", json={
                "report_date": "2026-10-13", "completed": "完成接口联调",
            }).json()
            final_text = report["draft"].replace(
                "完成接口联调", "完成接口联调；完成未经记录的生产发布", 1,
            )
            response = client.post(f"/api/reports/{report['id']}/confirm", json={"final_text": final_text})
            self.assertEqual(409, response.status_code)
            claim = next(item for item in response.json()["detail"]["review"]["issues"]
                         if item["type"] == "unsupported_claim")
            self.assertIn("完成未经记录的生产发布", claim["summary"])
            self.assertNotIn("完成接口联调；", claim["summary"])
            self.assertIn("完成接口联调", claim["evidence"])
            self.assertIn("片段", claim["suggestion"])
            self.assertNotIn("完成未经记录的生产发布", str(client.app.state.store.list_agent_runs(1)[0]))

            sensitive = report["draft"].replace(
                "完成接口联调", "完成接口联调；完成生产发布 token=sample-private-value", 1,
            )
            blocked = client.post(f"/api/reports/{report['id']}/confirm", json={"final_text": sensitive})
            self.assertEqual(409, blocked.status_code)
            self.assertTrue(blocked.json()["detail"]["review"]["unsafe"])
            self.assertNotIn("sample-private-value", blocked.text)

    def test_existing_database_gets_initial_snapshot_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with closing(sqlite3.connect(path)) as connection:
                with connection:
                    connection.execute("""CREATE TABLE reports (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, report_date TEXT NOT NULL,
                        origin TEXT NOT NULL DEFAULT 'generated', source_json TEXT NOT NULL,
                        draft TEXT NOT NULL, final_text TEXT, status TEXT NOT NULL,
                        strategy_version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    )""")
                    connection.execute("""INSERT INTO reports VALUES (
                        'old-report', 'daily', '2026-10-06', 'generated', '{}', '旧草稿', NULL,
                        'draft', 1, '2026-10-06', '2026-10-06'
                    )""")
            store = Store(path)
            self.assertEqual("旧草稿", store.get_report("old-report")["initial_draft"])


if __name__ == "__main__":
    unittest.main()
