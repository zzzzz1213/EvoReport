import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from evoreport.agent_runtime import (DailyQueue, DailyQueueFull, DailyQueueTimeout, MultiAgentRuntime, _critic_view, _fact_view, _gate_view,
                                     _planner_view, _replan_tasks, _revise_draft, _validate_plan,
                                     _writer_feedback, _writer_view)
from evoreport.evaluation import review_report
from evoreport.role_executor import RoleProcessExecutor
from evoreport.store import Store


class BrokenWriter:
    def rewrite(self, draft):
        return "# 工作日报\n\n## 今日完成\n- 无关内容\n", False, "测试用候选"


class FailingWriter:
    def rewrite(self, draft):
        raise RuntimeError("writer failed")


class CapturingWriter:
    def __init__(self):
        self.input = ""

    def rewrite(self, draft):
        self.input = draft
        return draft, False, "本地写作"


class CapturingRoleExecutor:
    def __init__(self):
        self.delegate = RoleProcessExecutor()
        self.calls = []

    def execute(self, role, operation, payload, timeout_seconds=None):
        self.calls.append((role, operation, payload))
        return self.delegate.execute(role, operation, payload, timeout_seconds)

    def close(self):
        self.delegate.close()


class DailyQueueTests(unittest.TestCase):
    def test_fifo_capacity_and_release_after_exception(self):
        queue = DailyQueue(max_waiting=2)
        order = []
        errors = []
        def waiter(number):
            try:
                with queue.acquire(5):
                    order.append(number)
                    if number == 1:
                        raise RuntimeError("test")
            except RuntimeError as exc:
                errors.append(str(exc))

        threads = []
        with queue.acquire(5):
            for number in (1, 2):
                thread = threading.Thread(target=waiter, args=(number,))
                thread.start()
                threads.append(thread)
                deadline = time.monotonic() + 2
                while queue.status()["waiting"] < number and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(number, queue.status()["waiting"])
            with self.assertRaises(DailyQueueFull):
                with queue.acquire(1):
                    pass
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual([1, 2], order)
        self.assertEqual(["test"], errors)
        self.assertEqual(0, queue.status()["running"])
        self.assertEqual(0, queue.status()["waiting"])

    def test_timeout_removes_waiter_and_allows_next_task(self):
        queue = DailyQueue(max_waiting=1)
        with queue.acquire(1):
            with self.assertRaises(DailyQueueTimeout):
                with queue.acquire(0.02):
                    pass
            self.assertEqual(0, queue.status()["waiting"])
        with queue.acquire(1):
            self.assertEqual(1, queue.status()["running"])


class AgentRuntimeTests(unittest.TestCase):
    def test_critic_reviews_source_and_writer_can_revise_twice(self):
        fact = "完成" + "接口联调" * 40
        source = {"kind": "daily", "facts": [fact], "projects": [], "strategy_version": 1}
        draft = (
            "# 工作日报\n\n## 今日完成\n- " + fact[:80] + "\n\n"
            "## 遇到的问题\n- 暂无已记录内容\n\n## 明日计划\n- 暂无已记录内容\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "runtime.db")
            report = store.create_report("daily", "2026-10-13", source, draft, 1)
            result = MultiAgentRuntime(store, BrokenWriter()).run_report(report["id"], source, draft)
            steps = store.list_agent_runs(1)[0]["steps"]
            replay = MultiAgentRuntime(store, FailingWriter()).replay_report(result["run_id"])

        self.assertEqual("passed", result["decision"])
        self.assertIn(fact, result["draft"])
        self.assertEqual([0, 1], [step["input"]["attempt"] for step in steps if step["agent_name"] == "Critic Agent"])
        self.assertEqual([0, 1], [step["input"]["attempt"] for step in steps if step["agent_name"] == "Writer Agent"])
        replans = [step for step in steps if step["agent_name"] == "Planner Agent" and
                   step["input"].get("attempt")]
        self.assertEqual(1, len(replans))
        self.assertEqual(["restore_headings", "restore_facts"], replans[0]["output"]["task_actions"])
        revised = [step for step in steps if step["agent_name"] == "Writer Agent" and step["input"]["attempt"] > 0]
        self.assertTrue(all(step["input"]["feedback_task_ids"] == ["T001"] for step in revised))
        self.assertEqual("Gate Agent", steps[-1]["agent_name"])
        self.assertNotIn(fact, str(steps))
        self.assertEqual("verified", replay["status"])

    def test_critic_does_not_need_reference_draft(self):
        source = {"facts": ["完成接口联调"]}
        result = review_report(source, "## 今日完成\n- 无关内容\n## 遇到的问题\n## 明日计划\n")
        self.assertFalse(result["passed"])
        self.assertEqual([0], result["missing_fact_indexes"])

    def test_redacted_placeholder_is_safe_but_plain_token_is_not(self):
        source = {"facts": []}
        headings = "## 今日完成\n## 遇到的问题\n## 明日计划\n"
        self.assertTrue(review_report(source, headings + "token=[REDACTED]")["passed"])
        self.assertFalse(review_report(source, headings + "token=example-value")["passed"])

    def test_failed_writer_closes_run_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "runtime.db")
            report = store.create_report("daily", "2026-10-13", {"facts": []}, "draft", 1)
            with self.assertRaises(RuntimeError):
                MultiAgentRuntime(store, FailingWriter()).run_report(report["id"], {"facts": []}, "draft")
            self.assertEqual("failed", store.list_agent_runs(1)[0]["status"])

    def test_planner_and_fact_handoff_groups_multi_project_evidence(self):
        facts = ["完成界面组件", "修复查询异常", "完成手工回归"]
        source = {
            "kind": "daily", "facts": facts,
            "fact_sources": [
                {"source_type": "git_commit", "project_id": "p1", "commit_hash": "a" * 40},
                {"source_type": "git_commit", "project_id": "p2", "commit_hash": "b" * 40},
                {"source_type": "manual", "line": 2},
            ],
            "projects": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}],
            "memory_context": {"project_notes": [{"value": "背景仅供规划"}]},
        }
        draft = "## 今日完成\n" + "\n".join(f"- {fact}" for fact in facts) + "\n## 遇到的问题\n## 明日计划\n"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "runtime.db")
            report = store.create_report("daily", "2026-10-13", source, draft, 1)
            writer = CapturingWriter()
            executor = CapturingRoleExecutor()
            result = MultiAgentRuntime(store, writer, executor).run_report(report["id"], source, draft)
            critic_source = next(payload["source"] for role, operation, payload in executor.calls
                                 if role == "Critic Agent" and operation == "review_draft")
            executor.close()
            run = store.get_agent_run(result["run_id"])
            handoff = store.get_agent_checkpoint(result["run_id"])["handoff"]

        self.assertEqual(["manual", "project", "project", "project"], run["steps"][0]["output"]["task_scopes"])
        fact_steps = [step for step in run["steps"] if step["agent_name"] == "Fact Agent"]
        self.assertEqual([task["id"] for task in handoff["plan"]["tasks"]],
                         [step["input"]["task_id"] for step in fact_steps])
        self.assertEqual(["F0001", "F0002", "F0003"], sorted(
            evidence_id for step in fact_steps for evidence_id in step["output"]["evidence_ids"]))
        critic_step = next(step for step in run["steps"] if step["agent_name"] == "Critic Agent")
        self.assertEqual(["F0001", "F0002", "F0003"], critic_step["input"]["evidence_ids"])
        self.assertEqual("p1", handoff["evidence"][0]["source_ref"]["project_id"])
        self.assertEqual([], handoff["plan"]["tasks"][-1]["fact_indexes"])
        self.assertEqual(["cover_facts", "cover_facts", "cover_facts", "context_only"],
                         [task["action"] for task in handoff["plan"]["tasks"]])
        self.assertEqual(5, handoff["contract_version"])
        self.assertEqual(["review_draft"], handoff["permissions"]["Critic Agent"]["operations"])
        self.assertNotIn("完成界面组件", str(handoff) + str(run["steps"]))
        self.assertNotIn("背景仅供规划", str(handoff) + str(run["steps"]))
        self.assertEqual(draft, writer.input)
        self.assertEqual({"kind", "facts", "fact_ids"}, set(critic_source))
        self.assertEqual(facts, critic_source["facts"])

    def test_writer_receives_missing_source_fact_from_handoff(self):
        fact = "完成接口联调"
        source = {"kind": "daily", "facts": [fact]}
        draft = "## 今日完成\n- 暂无已记录内容\n## 遇到的问题\n## 明日计划\n"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "runtime.db")
            report = store.create_report("daily", "2026-10-13", source, draft, 1)
            writer = CapturingWriter()
            result = MultiAgentRuntime(store, writer).run_report(report["id"], source, draft)
        self.assertIn("- 完成接口联调", writer.input)
        self.assertEqual("passed", result["decision"])
        self.assertEqual(["F0001"], review_report(source, "## 今日完成\n## 遇到的问题\n## 明日计划\n")["missing_evidence_ids"])

    def test_project_notes_prioritize_selected_projects_without_becoming_evidence(self):
        source = {
            "kind": "daily", "facts": ["完成前端联调", "修复接口异常"],
            "fact_sources": [
                {"source_type": "git_commit", "project_id": "p1", "commit_hash": "a" * 40},
                {"source_type": "git_commit", "project_id": "p2", "commit_hash": "b" * 40},
            ],
            "projects": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}],
            "memory_context": {"project_notes": [
                {"id": "note-p2", "project_id": "p2", "text": "关注接口兼容性"},
                {"id": "note-other", "project_id": "p4", "text": "其他项目背景"},
            ]},
        }
        draft = "## 今日完成\n## 遇到的问题\n## 明日计划\n"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "runtime.db")
            report = store.create_report("daily", "2026-10-13", source, draft, 1)
            writer = CapturingWriter()
            executor = CapturingRoleExecutor()
            result = MultiAgentRuntime(store, writer, executor).run_report(report["id"], source, draft)
            critic_source = next(payload["source"] for role, operation, payload in executor.calls
                                 if role == "Critic Agent" and operation == "review_draft")
            executor.close()
            run = store.get_agent_run(result["run_id"])
            handoff = store.get_agent_checkpoint(result["run_id"])["handoff"]
            replay = MultiAgentRuntime(store, FailingWriter()).replay_report(result["run_id"])

        tasks = handoff["plan"]["tasks"]
        self.assertEqual(["p2", "p1", "p3"], [task["project_id"] for task in tasks])
        self.assertEqual(["note-p2"], tasks[0]["context_note_ids"])
        self.assertNotIn("context_note_ids", tasks[1])
        self.assertEqual(["F0001", "F0002"], [item["id"] for item in handoff["evidence"]])
        self.assertLess(writer.input.index("修复接口异常"), writer.input.index("完成前端联调"))
        self.assertNotIn("关注接口兼容性", writer.input + str(handoff) + str(run["steps"]))
        self.assertEqual({"kind", "facts", "fact_ids"}, set(critic_source))
        self.assertEqual(source["facts"], critic_source["facts"])
        self.assertEqual(1, run["steps"][0]["output"]["project_notes"])
        self.assertEqual("verified", replay["status"])

    def test_content_plan_dependencies_and_role_inputs_are_limited(self):
        source = {"kind": "daily", "facts": ["完成内部接口调整"],
                  "fact_sources": [{"source_type": "manual", "line": 2}],
                  "projects": [{"id": "p1", "name": "项目甲"}],
                  "memory_context": {"project_notes": [{"id": "note-1", "project_id": "p1",
                                                         "text": "内部背景不应进入交接"}]},
                  "manual_problems": "阻塞原因正文", "manual_tomorrow": "明日计划正文"}
        draft = "## 今日完成\n- 完成内部接口调整\n## 遇到的问题\n- 阻塞原因正文\n## 明日计划\n- 明日计划正文\n"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "runtime.db")
            report = store.create_report("daily", "2026-10-13", source, draft, 1)
            result = MultiAgentRuntime(store, CapturingWriter()).run_report(report["id"], source, draft)
            plan = store.get_agent_checkpoint(result["run_id"])["handoff"]["plan"]
        tasks = plan["tasks"]
        self.assertEqual(["cover_facts", "context_only", "preserve_section", "preserve_section"],
                         [task["action"] for task in tasks])
        self.assertEqual([tasks[0]["id"]], tasks[2]["depends_on"])
        self.assertEqual([tasks[2]["id"]], tasks[3]["depends_on"])
        self.assertEqual([task["id"] for task in tasks], plan["execution_order"])

        planner = _planner_view(source)
        fact = _fact_view(source)
        writer = _writer_view(source)
        critic = _critic_view(source, [{"fact_index": 0, "id": "F0001"}])
        gate = _gate_view({"passed": False, "unsafe": False, "missing_fact_indexes": [],
                           "missing_headings": [], "unsupported_claims": [{"text": "私有片段"}]})
        self.assertNotIn("完成内部接口调整", str(planner) + str(fact))
        self.assertNotIn("内部背景不应进入交接", str(planner) + str(fact) + str(writer) + str(critic))
        self.assertNotIn("阻塞原因正文", str(planner) + str(fact))
        self.assertEqual({"facts"}, set(writer))
        self.assertEqual({"kind", "facts", "fact_ids"}, set(critic))
        feedback = _writer_feedback({"missing_fact_indexes": [], "missing_evidence_ids": [],
                                     "unsupported_claims": [{"text": "私有片段"}]})
        self.assertEqual({"missing_fact_indexes", "missing_evidence_ids", "missing_headings",
                          "unsupported_claims", "unsafe", "task_ids", "issue_types"}, set(feedback))
        self.assertEqual(["unsupported_claims"], feedback["issue_types"])
        self.assertNotIn("私有片段", str(gate))

    def test_dynamic_repair_plan_orders_issue_types_and_applies_targeted_fixes(self):
        plan = {"tasks": [{"id": "T001", "scope": "manual", "fact_indexes": [0],
                            "action": "cover_facts", "depends_on": []}],
                "execution_order": ["T001"],
                "required_sections": ["今日完成", "遇到的问题", "明日计划"]}
        feedback = {
            "missing_fact_indexes": [0], "missing_evidence_ids": ["F0001"],
            "missing_headings": ["明日计划"],
            "unsupported_claims": [{"line": 2, "column_start": 9, "column_end": 17}],
            "unsafe": True, "task_ids": ["T001"],
            "issue_types": ["sensitive_information", "unsupported_claims", "missing_headings", "missing_facts"],
        }
        repair = _replan_tasks(plan, {"T001": "completed"}, [], feedback, 1)
        self.assertEqual(["redact_sensitive", "remove_unsupported_claims", "restore_headings", "restore_facts"],
                         [node["action"] for node in repair["nodes"]])
        candidate = "## 今日完成\n- 无关内容；完成虚构发布 token=example-value\n## 遇到的问题\n"
        fixed = _revise_draft(candidate, candidate, {"facts": ["完成接口联调"]}, feedback, repair, 1)
        self.assertIn("完成接口联调", fixed)
        self.assertIn("## 明日计划", fixed)
        self.assertNotIn("example-value", fixed)
        completed_repair = {**repair,
                            "nodes": [{**node, "status": "completed"} for node in repair["nodes"]]}
        second_repair = _replan_tasks(plan, {"T001": "completed"}, [completed_repair], feedback, 2)
        self.assertEqual(repair["execution_order"], second_repair["superseded_task_ids"])
        self.assertTrue(all(node["id"].startswith("R2-") for node in second_repair["nodes"]))

    def test_task_graph_rejects_cycles_duplicate_facts_and_unknown_dependencies(self):
        base = {"required_sections": [], "execution_order": ["T001", "T002"]}
        with self.assertRaisesRegex(ValueError, "循环"):
            _validate_plan({**base, "tasks": [
                {"id": "T001", "fact_indexes": [0], "depends_on": ["T002"]},
                {"id": "T002", "fact_indexes": [], "depends_on": ["T001"]},
            ]}, 1)
        with self.assertRaisesRegex(ValueError, "覆盖"):
            _validate_plan({**base, "tasks": [
                {"id": "T001", "fact_indexes": [0], "depends_on": []},
                {"id": "T002", "fact_indexes": [0], "depends_on": []},
            ]}, 1)
        with self.assertRaisesRegex(ValueError, "依赖"):
            _validate_plan({**base, "tasks": [
                {"id": "T001", "fact_indexes": [0], "depends_on": []},
                {"id": "T002", "fact_indexes": [], "depends_on": ["T999"]},
            ]}, 1)


if __name__ == "__main__":
    unittest.main()
