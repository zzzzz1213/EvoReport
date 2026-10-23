from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict

from .evaluation import review_report
from .agent_instructions import instruction_hash
from .llm import ArkReportWriter, MODEL_CALL_SECONDS, call_with_deadline
from .report_generator import redact_sensitive
from .role_executor import ROLE_PROTOCOL_VERSION, RoleProcessExecutor
from .store import Store


ROLE_CONTRACT_VERSION = 5
ROLE_PERMISSIONS = {
    "Planner Agent": {"reads": ["fact_count", "source_locations", "project_ids", "note_ids", "section_presence"],
                      "operations": ["plan_tasks", "replan_tasks"]},
    "Fact Agent": {"reads": ["fact_count", "source_locations", "task_plan", "task_dependencies"],
                   "operations": ["execute_task"]},
    "Writer Agent": {"reads": ["initial_draft", "candidate_draft", "fact_text", "evidence_ids", "task_plan",
                                 "critic_feedback", "repair_plan"],
                     "operations": ["write_draft", "revise_draft"]},
    "Critic Agent": {"reads": ["candidate_draft", "evidence_ids", "fact_text"],
                     "operations": ["review_draft"]},
    "Gate Agent": {"reads": ["review_summary"], "operations": ["decide"]},
}


class RunBudgetExceeded(TimeoutError):
    pass


class DailyQueueFull(RuntimeError):
    pass


class DailyQueueTimeout(TimeoutError):
    pass


class DailyQueue:
    def __init__(self, max_waiting: int = 5):
        self._condition = threading.Condition()
        self._waiting = deque()
        self._running = False
        self.max_waiting = max_waiting

    def status(self) -> Dict[str, int]:
        with self._condition:
            return {"running": int(self._running), "waiting": len(self._waiting),
                    "max_running": 1, "max_waiting": self.max_waiting}

    @contextmanager
    def acquire(self, timeout: float):
        started = time.perf_counter()
        ticket = object()
        with self._condition:
            if (self._running or self._waiting) and len(self._waiting) >= self.max_waiting:
                raise DailyQueueFull("日报队列已满，请稍后重试")
            self._waiting.append(ticket)
            try:
                while True:
                    remaining = timeout - (time.perf_counter() - started)
                    if remaining <= 0:
                        raise DailyQueueTimeout("日报排队等待超时，请稍后重试")
                    if not self._running and self._waiting[0] is ticket:
                        self._waiting.popleft()
                        self._running = True
                        break
                    self._condition.wait(remaining)
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
        try:
            yield started
        finally:
            with self._condition:
                self._running = False
                self._condition.notify_all()


class MultiAgentRuntime:
    """协调可验证的本地角色节点，并持久化不含正文的执行摘要。"""

    MAX_REVISIONS = 2
    MAX_RUN_SECONDS = 45
    MAX_TOTAL_SECONDS = 90
    MAX_TOTAL_TOKENS = 12000
    MAX_RECOVERIES = 2
    STALE_SECONDS = 60
    REPLAY_VERSION = 11

    def __init__(self, store: Store, writer: ArkReportWriter, role_executor: RoleProcessExecutor | None = None):
        self.store = store
        self.writer = writer
        self.role_executor = role_executor or RoleProcessExecutor()
        self.daily_queue = DailyQueue()

    def run_report(self, report_id: str, source: Dict[str, Any], draft: str) -> Dict[str, Any]:
        with self.daily_queue.acquire(self.MAX_RUN_SECONDS) as started:
            return self._run_report(report_id, source, draft, started)

    def _run_report(self, report_id: str, source: Dict[str, Any], draft: str, started: float) -> Dict[str, Any]:
        prompt = self.store.active_writer_prompt()
        critic_rule = self.store.active_critic_rule()
        run_id = self.store.start_agent_run("daily_report", report_id, {
            "writer_prompt": {"version": prompt["version"], "sha256": prompt["sha256"]},
            "critic_rules": {"version": critic_rule["version"], "fingerprint": critic_rule["fingerprint"]},
        })
        return self._execute_daily(run_id, source, draft, recovery_count=0, started=started)

    def recoverable_runs(self) -> list[Dict[str, Any]]:
        runs = self.store.list_recoverable_agent_runs(self.STALE_SECONDS, self.MAX_RECOVERIES)
        result = []
        for run in runs:
            report = self.store.get_report(run["report_id"])
            checkpoint = self.store.get_agent_checkpoint(run["id"])
            if not report or report["status"] != "draft" or report["draft"] != report["initial_draft"]:
                continue
            if checkpoint is None and self.store.get_agent_run(run["id"])["steps"]:
                continue
            if checkpoint and checkpoint["input_hash"] != _input_hash(report["source"], report["initial_draft"]):
                continue
            run["remaining_budget_ms"] = max(
                0, self.MAX_TOTAL_SECONDS * 1000 - run["duration_ms"] - _stale_charge_ms(run, self.MAX_RUN_SECONDS)
            )
            run["remaining_tokens"] = max(0, self.MAX_TOTAL_TOKENS - run["token_used"])
            result.append(run)
        return result

    def resume_report(self, run_id: str) -> Dict[str, Any]:
        initial = self.store.get_agent_run(run_id)
        if not initial or initial["workflow"] != "daily_report":
            raise ValueError("可恢复的日报运行不存在")
        charge_ms = _stale_charge_ms(initial, self.MAX_RUN_SECONDS)
        remaining = min(self.MAX_RUN_SECONDS,
                        self.MAX_TOTAL_SECONDS - (initial["duration_ms"] + charge_ms) / 1000)
        if remaining <= 0:
            remaining = self.MAX_RUN_SECONDS
        with self.daily_queue.acquire(remaining) as started:
            return self._resume_report(run_id, charge_ms, started)

    def _resume_report(self, run_id: str, charge_ms: int, started: float) -> Dict[str, Any]:
        run = self.store.get_agent_run(run_id)
        if not run or run["workflow"] != "daily_report":
            raise ValueError("可恢复的日报运行不存在")
        report = self.store.get_report(run["report_id"])
        if not report or report["status"] != "draft" or report["draft"] != report["initial_draft"]:
            raise ValueError("草稿已修改或确认，不能恢复运行")
        checkpoint = self.store.get_agent_checkpoint(run_id)
        if checkpoint is None and run["steps"]:
            raise ValueError("旧运行缺少检查点，不能安全恢复")
        if checkpoint and checkpoint["stage"] not in {
            "planned", "facts_ready", "writer_ready", "review_ready", "replanned", "gated",
        }:
            raise ValueError("检查点阶段无效，不能恢复运行")
        if checkpoint and checkpoint["input_hash"] != _input_hash(report["source"], report["initial_draft"]):
            raise ValueError("事实或初稿快照已变化，不能恢复运行")
        if not self.store.claim_agent_run(run_id, self.STALE_SECONDS, self.MAX_RECOVERIES, charge_ms):
            raise ValueError("运行仍在执行、已结束或超过恢复次数上限")
        claimed = self.store.get_agent_run(run_id)
        if claimed["duration_ms"] >= self.MAX_TOTAL_SECONDS * 1000 or claimed["token_used"] >= self.MAX_TOTAL_TOKENS:
            self.store.finish_agent_run(
                run_id, "budget_exhausted", claimed["duration_ms"], "RunBudgetExceeded",
                claimed["recovery_count"],
            )
            raise ValueError("整次任务预算已耗尽，不能继续恢复")
        return self._execute_daily(
            run_id, report["source"], report["initial_draft"], checkpoint,
            claimed["duration_ms"], claimed["recovery_count"], started,
        )

    def _execute_daily(
        self, run_id: str, source: Dict[str, Any], draft: str,
        checkpoint: Dict[str, Any] | None = None, prior_duration_ms: int = 0,
        recovery_count: int = 0, started: float | None = None,
    ) -> Dict[str, Any]:
        started = time.perf_counter() if started is None else started
        input_hash = _input_hash(source, draft)
        source_hash = _source_hash(source)
        stage = checkpoint["stage"] if checkpoint else "start"
        attempt = checkpoint["attempt"] if checkpoint else 0
        candidate = redact_sensitive(checkpoint["candidate_draft"] if checkpoint else draft)
        review = checkpoint["review"] if checkpoint else {}
        llm_used = checkpoint["llm_used"] if checkpoint else False
        handoff = checkpoint.get("handoff", {}) if checkpoint else {}
        if stage != "start" and handoff.get("contract_version") != ROLE_CONTRACT_VERSION:
            handoff = _daily_handoff(source, stage != "planned")

        def check_budget() -> None:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if self.store.get_agent_run(run_id)["token_used"] >= self.MAX_TOTAL_TOKENS:
                raise RunBudgetExceeded("整次日报运行超过 Token 预算")
            if prior_duration_ms + elapsed_ms >= self.MAX_TOTAL_SECONDS * 1000:
                raise RunBudgetExceeded("整次日报运行超过总时间预算")
            if elapsed_ms >= self.MAX_RUN_SECONDS * 1000:
                raise TimeoutError("日报运行超过时间预算")

        def run_role(role: str, operation: str, payload: Dict[str, Any]) -> tuple[Any, Dict[str, Any]]:
            check_budget()
            elapsed = time.perf_counter() - started
            remaining_seconds = min(
                self.MAX_RUN_SECONDS - elapsed,
                self.MAX_TOTAL_SECONDS - prior_duration_ms / 1000 - elapsed,
            )
            return self.role_executor.execute(role, operation, payload, remaining_seconds)

        def record(
            name: str, status: str, input_summary: Dict[str, Any], output_summary: Dict[str, Any],
            metrics: Dict[str, Any], next_stage: str, step_started: float,
            execution: Dict[str, Any] | None = None,
        ) -> None:
            check_budget()
            if name == "Writer Agent":
                output_summary = {**output_summary, "candidate_sha256": _text_hash(candidate)}
            if execution:
                output_summary = {**output_summary, "execution": execution}
            self.store.add_agent_step(
                run_id, name, status, input_summary, output_summary, metrics, _elapsed_ms(step_started),
                checkpoint={
                    "stage": next_stage, "attempt": attempt, "candidate_draft": candidate,
                    "review": review, "handoff": handoff, "input_hash": input_hash, "llm_used": llm_used,
                    "source_hash": source_hash, "replay_version": self.REPLAY_VERSION,
                },
                expected_recovery_count=recovery_count,
                cumulative_duration_ms=prior_duration_ms + _elapsed_ms(started),
            )

        try:
            critic_rule = self.store.critic_rule_for_snapshot(
                self.store.get_agent_run(run_id)["instruction_snapshot"])
            if checkpoint and not _valid_daily_handoff(source, handoff, stage):
                raise ValueError("任务交接包与来源或角色权限不一致")
            if stage == "start":
                check_budget()
                step_started = time.perf_counter()
                plan, execution = run_role("Planner Agent", "plan_tasks", {"source": _planner_view(source)})
                handoff = _daily_handoff(source, False, plan)
                _require_role_operation(handoff, "Planner Agent", "plan_tasks")
                record("Planner Agent", "passed", {"workflow": "daily_report"},
                       {"max_revisions": self.MAX_REVISIONS, "strategy_version": source.get("strategy_version"),
                        "personal_preferences": len(source.get("memory_context", {}).get("preferences", {})),
                        "project_notes": sum(len(task.get("context_note_ids", []))
                                             for task in handoff["plan"]["tasks"]),
                        "task_ids": [task["id"] for task in handoff["plan"]["tasks"]],
                        "task_scopes": [task["scope"] for task in handoff["plan"]["tasks"]],
                        "task_actions": [task["action"] for task in handoff["plan"]["tasks"]],
                        "task_dependencies": [task["depends_on"] for task in handoff["plan"]["tasks"]],
                        "focused_sections": handoff["plan"].get("section_focus", []),
                        "role_contract_version": ROLE_CONTRACT_VERSION},
                       {}, "planned", step_started, execution)
                stage = "planned"
            if stage == "planned":
                _require_role_operation(handoff, "Fact Agent", "execute_task")
                while stage == "planned":
                    check_budget()
                    step_started = time.perf_counter()
                    task = _next_ready_task(handoff)
                    if task is None:
                        if handoff["plan"]["tasks"]:
                            raise ValueError("任务图存在循环依赖或无法满足的前置任务")
                        _, execution = run_role("Fact Agent", "execute_task", {
                            "source": _fact_view(source), "plan": handoff["plan"], "task": {"id": "NONE"},
                        })
                        record("Fact Agent", "passed", {"task_id": None, "depends_on": []},
                               {"task_id": None, "action": "no_tasks", "task_status": "completed",
                                "prepared_fact_count": 0, "evidence_ids": []},
                               {"completed_tasks": 0, "total_tasks": 0}, "facts_ready", step_started, execution)
                        stage = "facts_ready"
                        break
                    task_evidence, execution = run_role("Fact Agent", "execute_task", {
                        "source": _fact_view(source), "plan": handoff["plan"], "task": task,
                    })
                    handoff["evidence"].extend(task_evidence)
                    handoff["evidence"].sort(key=lambda item: item["fact_index"])
                    handoff["task_states"][task["id"]] = "completed"
                    completed = sum(value == "completed" for value in handoff["task_states"].values())
                    next_stage = "facts_ready" if completed == len(handoff["plan"]["tasks"]) else "planned"
                    record("Fact Agent", "passed",
                           {"task_id": task["id"], "action": task["action"],
                            "depends_on": task["depends_on"]},
                           {"task_id": task["id"], "action": task["action"], "task_status": "completed",
                            "prepared_fact_count": len(task_evidence),
                            "evidence_ids": [item["id"] for item in task_evidence]},
                           {"completed_tasks": completed, "total_tasks": len(handoff["plan"]["tasks"])},
                           next_stage, step_started, execution)
                    stage = next_stage
            while stage != "gated":
                check_budget()
                if stage == "facts_ready":
                    step_started = time.perf_counter()
                    _require_role_operation(handoff, "Writer Agent", "write_draft")
                    writer_input, execution = run_role("Writer Agent", "write_draft", {
                        "draft": draft, "source": _writer_view(source), "evidence": handoff["evidence"],
                        "plan": handoff["plan"],
                    })
                    instruction = self._writer_instruction_for_run(run_id)
                    usage = None
                    if isinstance(self.writer, ArkReportWriter) and self.writer.enabled:
                        current = self.store.get_agent_run(run_id)["token_used"]
                        remaining = self.MAX_TOTAL_TOKENS - current
                        input_estimate = self.writer.estimate_input_tokens(writer_input, instruction)
                        if remaining <= input_estimate:
                            raise RunBudgetExceeded("剩余 Token 不足以调用 Writer")
                        output_limit = min(2048, remaining - input_estimate)
                        reserved = input_estimate + output_limit
                        self.store.update_agent_tokens(run_id, recovery_count, current, reserved, reserved,
                                                       self.MAX_TOTAL_TOKENS)
                        remaining_seconds = min(
                            MODEL_CALL_SECONDS,
                            self.MAX_RUN_SECONDS - (time.perf_counter() - started),
                            self.MAX_TOTAL_SECONDS - prior_duration_ms / 1000 - (time.perf_counter() - started),
                        )
                        writer = self.writer
                        candidate, llm_used, writer_note, usage = call_with_deadline(
                            lambda: writer.rewrite_with_usage(
                                writer_input, instruction=instruction, max_output_tokens=output_limit,
                            ), remaining_seconds,
                        )
                        if usage is not None:
                            actual = usage["total_tokens"]
                            self.store.update_agent_tokens(
                                run_id, recovery_count, current + reserved, actual - reserved,
                                (actual if usage["source"] == "estimated" else 0) - reserved,
                            )
                            if current + actual >= self.MAX_TOTAL_TOKENS:
                                raise RunBudgetExceeded("整次日报运行超过 Token 预算")
                    else:
                        candidate, llm_used, writer_note = (
                            self.writer.rewrite(writer_input, instruction=instruction)
                            if isinstance(self.writer, ArkReportWriter) else self.writer.rewrite(writer_input)
                        )
                    candidate = redact_sensitive(candidate)
                    record("Writer Agent", "passed", {"attempt": 0, "strategy_version": source.get("strategy_version"),
                                                      "evidence_ids": [item["id"] for item in handoff["evidence"]],
                                                      "writer_input_sha256": _text_hash(writer_input)},
                           {"draft_characters": len(candidate), "section_count": candidate.count("## "),
                            "llm_used": llm_used, "note": writer_note},
                           {"token_usage": usage} if usage else {}, "writer_ready", step_started, execution)
                    stage = "writer_ready"
                if stage == "writer_ready":
                    check_budget()
                    step_started = time.perf_counter()
                    _require_role_operation(handoff, "Critic Agent", "review_draft")
                    critic_source = _critic_view(source, handoff["evidence"])
                    review, execution = run_role("Critic Agent", "review_draft", {
                        "source": critic_source, "candidate": candidate, "rule": critic_rule,
                    })
                    critic_feedback = _writer_feedback(review, handoff["evidence"])
                    critic_status = "passed" if review["passed"] else "needs_review"
                    record("Critic Agent", critic_status, {"attempt": attempt,
                                                          "evidence_ids": critic_source["fact_ids"]},
                           {"missing_fact_indexes": review["missing_fact_indexes"],
                            "missing_evidence_ids": review["missing_evidence_ids"],
                            "missing_headings": review["missing_headings"], "unsafe": review["unsafe"],
                            "unsupported_claim_lines": [item["line"] for item in review["unsupported_claims"]],
                            "issue_types": critic_feedback["issue_types"],
                            "affected_task_ids": critic_feedback["task_ids"]},
                           review["metrics"], "review_ready", step_started, execution)
                    stage = "review_ready"
                if stage == "review_ready":
                    if review["passed"] or attempt == self.MAX_REVISIONS:
                        step_started = time.perf_counter()
                        _require_role_operation(handoff, "Gate Agent", "decide")
                        gate_input = _gate_view(review)
                        gate_result, execution = run_role("Gate Agent", "decide", {
                            "review": gate_input, "mode": "daily", "warnings_acknowledged": False,
                        })
                        decision = gate_result["decision"]
                        record("Gate Agent", decision,
                               {"critic_status": "passed" if review["passed"] else "needs_review",
                                "review_attempts": attempt + 1},
                               {"decision": "允许进入人工编辑" if decision == "passed" else "建议人工检查后再确认"},
                               {"ready_for_review": 1.0 if decision == "passed" else 0.0},
                               "gated", step_started, execution)
                        stage = "gated"
                    else:
                        check_budget()
                        step_started = time.perf_counter()
                        attempt += 1
                        feedback = _writer_feedback(review, handoff["evidence"])
                        _require_role_operation(handoff, "Planner Agent", "replan_tasks")
                        repair_plan, execution = run_role("Planner Agent", "replan_tasks", {
                            "plan": handoff["plan"], "task_states": handoff["task_states"],
                            "plan_revisions": handoff["plan_revisions"], "feedback": feedback,
                            "attempt": attempt,
                        })
                        handoff["plan_revisions"].append(repair_plan)
                        record("Planner Agent", "passed",
                               {"attempt": attempt, "critic_issue_types": feedback["issue_types"]},
                               {"attempt": attempt,
                                "added_task_ids": repair_plan["execution_order"],
                                "task_actions": [node["action"] for node in repair_plan["nodes"]],
                                "superseded_task_ids": repair_plan["superseded_task_ids"],
                                "role_contract_version": ROLE_CONTRACT_VERSION},
                               {"added_tasks": len(repair_plan["nodes"]),
                                "superseded_tasks": len(repair_plan["superseded_task_ids"])},
                               "replanned", step_started, execution)
                        stage = "replanned"
                if stage == "replanned":
                    check_budget()
                    step_started = time.perf_counter()
                    feedback = _writer_feedback(review, handoff["evidence"])
                    repair_plan = handoff["plan_revisions"][-1]
                    _require_role_operation(handoff, "Writer Agent", "revise_draft")
                    candidate, execution = run_role("Writer Agent", "revise_draft", {
                        "draft": candidate, "initial_draft": draft, "source": _writer_view(source),
                        "feedback": feedback, "repair_plan": repair_plan, "revision": attempt,
                    })
                    for node in repair_plan["nodes"]:
                        node["status"] = "completed"
                    llm_used = False
                    record("Writer Agent", "passed",
                               {"attempt": attempt, "feedback_missing_facts": len(review["missing_fact_indexes"]),
                                "feedback_missing_headings": len(review["missing_headings"]),
                                "feedback_unsafe": review["unsafe"],
                                "feedback_task_ids": feedback["task_ids"],
                                "repair_task_ids": repair_plan["execution_order"],
                                "repair_actions": [node["action"] for node in repair_plan["nodes"]],
                                "writer_input_sha256": _text_hash(candidate)},
                           {"revision": "按返工任务修订" if attempt == 1 else "保守初稿恢复并修订",
                            "draft_characters": len(candidate)}, {}, "writer_ready", step_started, execution)
                    stage = "writer_ready"
            decision = "passed" if review["passed"] else "needs_review"
            completed_draft = candidate if decision == "passed" else draft
            check_budget()
            report = self.store.complete_daily_run(
                run_id, decision, completed_draft, draft, prior_duration_ms + _elapsed_ms(started),
                recovery_count,
            )
            return {"run_id": run_id, "decision": decision, "critic": review,
                    "draft": report["draft"], "llm_used": llm_used and attempt == 0 and decision == "passed"}
        except Exception as exc:
            self.store.finish_agent_run(
                run_id, "budget_exhausted" if isinstance(exc, RunBudgetExceeded) else "failed",
                prior_duration_ms + _elapsed_ms(started), type(exc).__name__,
                recovery_count,
            )
            raise

    def _writer_instruction_for_run(self, run_id: str) -> str:
        run = self.store.get_agent_run(run_id)
        snapshot = run["instruction_snapshot"].get("writer_prompt", {})
        version = snapshot.get("version", 1)
        prompt = self.store.get_writer_prompt(version)
        if prompt is None or (snapshot and (prompt["sha256"] != snapshot.get("sha256") or
                                               instruction_hash(prompt["instruction"]) != snapshot["sha256"])):
            raise ValueError("运行绑定的 Writer 指令版本不存在或内容已变化")
        return prompt["instruction"]

    def replay_report(self, run_id: str) -> Dict[str, Any]:
        run = self.store.get_agent_run(run_id)
        if not run or run["workflow"] not in {"daily_report", "final_review", "strategy_evaluation"}:
            raise ValueError("可回放的运行不存在")
        if run["workflow"] == "final_review":
            return self._replay_final_review(run)
        if run["workflow"] == "strategy_evaluation":
            return self._replay_strategy_evaluation(run)
        report = self.store.get_report(run["report_id"])
        try:
            snapshots = {item["step_id"]: item for item in self.store.list_agent_step_snapshots(run_id)}
        except (ValueError, json.JSONDecodeError):
            return {"run_id": run_id, "status": "unavailable", "reason": "回放快照已损坏", "steps": []}
        if (not report or not run["steps"] or
                set(snapshots) != {step["id"] for step in run["steps"]}):
            return {"run_id": run_id, "status": "unavailable", "reason": "旧运行缺少逐步快照", "steps": []}
        try:
            self._writer_instruction_for_run(run_id)
            critic_rule = self.store.critic_rule_for_snapshot(run["instruction_snapshot"])
        except ValueError:
            return {"run_id": run_id, "status": "unavailable", "reason": "运行绑定的指令或 Critic 规则版本缺失", "steps": []}
        if any(item["replay_version"] != self.REPLAY_VERSION for item in snapshots.values()):
            return {"run_id": run_id, "status": "unavailable", "reason": "审查规则版本不兼容", "steps": []}

        source = report["source"]
        source_hash = _source_hash(source)
        plan = _build_plan(_planner_view(source))
        evidence = _prepare_evidence(_fact_view(source), plan)
        critic_source = _critic_view(source, evidence)
        replay_handoff = _daily_handoff(source, False)
        results = []
        last_candidate = None
        last_review = None
        initial_candidate = None
        for step in run["steps"]:
            snapshot = snapshots[step["id"]]
            reason = ""
            if not _valid_execution(step["output"].get("execution")):
                reason = "角色进程执行证据缺失或无效"
            elif snapshot["source_hash"] != source_hash:
                reason = "来源快照已变化"
            elif step["agent_name"] == "Planner Agent":
                planner_attempt = step["input"].get("attempt")
                if planner_attempt is None:
                    initial_candidate = snapshot["candidate_draft"]
                    if snapshot["stage"] != "planned" or snapshot["handoff"] != replay_handoff:
                        reason = "任务或证据交接不一致"
                    elif (step["output"].get("task_ids") != [item["id"] for item in plan["tasks"]] or
                            step["output"].get("task_scopes") != [item["scope"] for item in plan["tasks"]] or
                            step["output"].get("task_actions") != [item["action"] for item in plan["tasks"]] or
                            step["output"].get("task_dependencies") != [item["depends_on"] for item in plan["tasks"]] or
                            step["output"].get("role_contract_version") != ROLE_CONTRACT_VERSION):
                        reason = "任务计划摘要不一致"
                elif last_review is None:
                    reason = "返工计划缺少 Critic 反馈"
                else:
                    feedback = _writer_feedback(last_review, evidence)
                    try:
                        repair_plan = _replan_tasks(
                            plan, replay_handoff["task_states"], replay_handoff["plan_revisions"],
                            feedback, planner_attempt,
                        )
                    except ValueError:
                        repair_plan = None
                    if repair_plan is None:
                        reason = "返工任务计划无效"
                    else:
                        replay_handoff["plan_revisions"].append(repair_plan)
                        if (snapshot["stage"] != "replanned" or snapshot["handoff"] != replay_handoff or
                                step["input"].get("critic_issue_types") != feedback["issue_types"] or
                                step["output"].get("added_task_ids") != repair_plan["execution_order"] or
                                step["output"].get("task_actions") !=
                                [node["action"] for node in repair_plan["nodes"]] or
                                step["output"].get("superseded_task_ids") !=
                                repair_plan["superseded_task_ids"] or
                                step["output"].get("role_contract_version") != ROLE_CONTRACT_VERSION):
                            reason = "返工任务图或摘要不一致"
            elif step["agent_name"] == "Fact Agent":
                task = _next_ready_task(replay_handoff)
                task_id = step["input"].get("task_id")
                task_evidence = [] if task is None else _execute_plan_task(_fact_view(source), plan, task)
                if task is None and plan["tasks"]:
                    reason = "任务图执行顺序不一致"
                elif task is not None and task_id != task["id"]:
                    reason = "任务未按依赖顺序执行"
                elif task is not None and (step["input"].get("action") != task["action"] or
                                            step["input"].get("depends_on") != task["depends_on"]):
                    reason = "任务输入与计划不一致"
                else:
                    if task is not None:
                        replay_handoff["evidence"].extend(task_evidence)
                        replay_handoff["evidence"].sort(key=lambda item: item["fact_index"])
                        replay_handoff["task_states"][task["id"]] = "completed"
                    completed = sum(value == "completed" for value in replay_handoff["task_states"].values())
                    expected_stage = "facts_ready" if completed == len(plan["tasks"]) else "planned"
                    if (snapshot["stage"] != expected_stage or snapshot["handoff"] != replay_handoff or
                            step["output"].get("task_id") != task_id or
                            step["output"].get("task_status") != "completed" or
                            step["output"].get("evidence_ids") != [item["id"] for item in task_evidence] or
                            step["output"].get("prepared_fact_count") != len(task_evidence) or
                            step["metrics"].get("completed_tasks") != completed or
                            step["metrics"].get("total_tasks") != len(plan["tasks"])):
                        reason = "任务执行结果或检查点不一致"
                if reason:
                    pass
                elif step["output"].get("action") != (task["action"] if task else "no_tasks"):
                    reason = "证据摘要不一致"
            elif step["agent_name"] == "Writer Agent":
                writer_attempt = step["input"].get("attempt")
                feedback = _writer_feedback(last_review, evidence) if last_review is not None else None
                if writer_attempt == 0 and initial_candidate is not None:
                    expected_input = _prepare_writer_draft(initial_candidate, _writer_view(source), evidence, plan)
                elif (initial_candidate is not None and last_candidate is not None and feedback is not None and
                      isinstance(writer_attempt, int) and writer_attempt > 0 and
                      replay_handoff["plan_revisions"]):
                    repair_plan = replay_handoff["plan_revisions"][-1]
                    expected_input = _revise_draft(
                        last_candidate, initial_candidate, _writer_view(source), feedback,
                        repair_plan, writer_attempt,
                    )
                    for node in repair_plan["nodes"]:
                        node["status"] = "completed"
                else:
                    expected_input = None
                if snapshot["stage"] != "writer_ready" or snapshot["handoff"] != replay_handoff:
                    reason = "任务或证据交接不一致"
                if not reason:
                    if expected_input is None or step["input"].get("writer_input_sha256") != _text_hash(expected_input):
                        reason = "Writer 输入与计划不一致"
                    elif writer_attempt > 0 and step["input"].get("feedback_task_ids") != _writer_feedback(
                            last_review, evidence)["task_ids"]:
                        reason = "Writer 返工任务与审查结果不一致"
                    elif writer_attempt > 0 and (
                            step["input"].get("repair_task_ids") != repair_plan["execution_order"] or
                            step["input"].get("repair_actions") !=
                            [node["action"] for node in repair_plan["nodes"]]):
                        reason = "Writer 执行的返工计划不一致"
                    elif writer_attempt > 0 and snapshot["candidate_draft"] != expected_input:
                        reason = "Writer 返工快照不一致"
                    elif (step["output"].get("candidate_sha256") != _text_hash(snapshot["candidate_draft"])
                            or step["output"].get("draft_characters") != len(snapshot["candidate_draft"])
                            or ("section_count" in step["output"] and
                                step["output"]["section_count"] != snapshot["candidate_draft"].count("## "))):
                        reason = "Writer 候选快照与摘要不一致"
                last_candidate = snapshot["candidate_draft"]
                last_review = None
            elif step["agent_name"] == "Critic Agent":
                if snapshot["stage"] != "review_ready" or snapshot["handoff"] != replay_handoff:
                    reason = "任务或证据交接不一致"
                elif last_candidate != snapshot["candidate_draft"]:
                    reason = "Critic 输入与 Writer 输出不一致"
                else:
                    last_review = _review_with_critic_skill(critic_source, last_candidate, critic_rule)
                    expected_output = {
                        "missing_fact_indexes": last_review["missing_fact_indexes"],
                        "missing_evidence_ids": last_review["missing_evidence_ids"],
                        "missing_headings": last_review["missing_headings"],
                        "unsafe": last_review["unsafe"],
                        "unsupported_claim_lines": [item["line"] for item in last_review["unsupported_claims"]],
                        "issue_types": _writer_feedback(last_review, evidence)["issue_types"],
                        "affected_task_ids": _writer_feedback(last_review, evidence)["task_ids"],
                    }
                    if (snapshot["review"] != last_review or
                            any(step["output"].get(key) != value for key, value in expected_output.items()) or
                            step["status"] != ("passed" if last_review["passed"] else "needs_review")):
                        reason = "Critic 审查结果不一致"
            elif step["agent_name"] == "Gate Agent":
                expected = "passed" if last_review and last_review["passed"] else "needs_review"
                expected_message = "允许进入人工编辑" if expected == "passed" else "建议人工检查后再确认"
                if snapshot["stage"] != "gated" or snapshot["handoff"] != replay_handoff:
                    reason = "任务或证据交接不一致"
                elif (last_review is None or snapshot["review"] != last_review or
                        step["status"] != expected or step["output"].get("decision") != expected_message):
                    reason = "Gate 决策不一致"
            else:
                reason = "未知角色步骤"
            results.append({"step_id": step["id"], "agent_name": step["agent_name"],
                            "result": "diverged" if reason else "verified", "reason": reason})
        return {"run_id": run_id,
                "status": "diverged" if any(item["result"] == "diverged" for item in results) else "verified",
                "reason": "", "steps": results}

    def _replay_final_review(self, run: Dict[str, Any]) -> Dict[str, Any]:
        run_id = run["id"]
        unavailable = lambda reason: {"run_id": run_id, "status": "unavailable", "reason": reason, "steps": []}
        report = self.store.get_report(run["report_id"])
        if not report or report["status"] != "confirmed" or not report["final_text"] or run["status"] != "passed":
            return unavailable("仅已确认且通过的终稿审查可回放")
        try:
            critic_rule = self.store.critic_rule_for_snapshot(run["instruction_snapshot"])
        except ValueError:
            return unavailable("审查规则版本不兼容")
        steps = run["steps"]
        if ([step["agent_name"] for step in steps] != ["Critic Agent", "Gate Agent"] or
                not steps[0]["input"].get("source_sha256") or
                not steps[0]["input"].get("final_sha256") or
                not steps[1]["input"].get("final_sha256")):
            return unavailable("旧运行缺少终稿审查输入指纹")
        if any(isinstance(step["output"].get("execution"), dict) and
               step["output"]["execution"].get("protocol_version") != ROLE_PROTOCOL_VERSION
               for step in steps):
            return unavailable("角色协议版本不兼容")
        critic_step, gate_step = steps
        source = report["source"]
        final_text = report["final_text"]
        source_hash = _source_hash(source)
        final_hash = _text_hash(final_text)
        if critic_step["input"]["source_sha256"] != source_hash:
            reason = "来源快照已变化"
        elif (critic_step["input"]["final_sha256"] != final_hash or
              gate_step["input"]["final_sha256"] != final_hash):
            reason = "终稿与审查输入不一致"
        else:
            reason = ""
        if reason:
            return {"run_id": run_id, "status": "diverged", "reason": "", "steps": [
                {"step_id": critic_step["id"], "agent_name": "Critic Agent", "result": "diverged", "reason": reason},
                {"step_id": gate_step["id"], "agent_name": "Gate Agent", "result": "diverged",
                 "reason": "审查输入不一致"},
            ]}
        review = _review_with_critic_skill(source, final_text, critic_rule)
        has_warnings = bool(review["missing_fact_indexes"] or review["missing_headings"] or review["unsupported_claims"])
        critic_status = "passed" if not review["unsafe"] and not has_warnings else "needs_review"
        expected_output = {
            "missing_fact_indexes": review["missing_fact_indexes"],
            "missing_evidence_ids": review["missing_evidence_ids"],
            "missing_headings": review["missing_headings"], "unsafe": review["unsafe"],
            "unsupported_claim_lines": [item["line"] for item in review["unsupported_claims"]],
        }
        critic_valid = (critic_step["status"] == critic_status and
                        critic_step["input"].get("fact_count") == len(source.get("facts", [])) and
                        critic_step["input"].get("final_characters") == len(final_text) and
                        all(critic_step["output"].get(key) == value for key, value in expected_output.items()) and
                        _valid_execution(critic_step["output"].get("execution")) and
                        critic_step["metrics"] == review["metrics"])
        acknowledged = gate_step["input"].get("warnings_acknowledged")
        decision = "blocked" if review["unsafe"] else "passed" if not has_warnings or acknowledged else "needs_review"
        expected_message = ("阻止敏感信息入库" if decision == "blocked" else
                            "等待用户确认审查提示" if decision == "needs_review" else "允许确认终稿")
        gate_valid = (gate_step["input"].get("critic_status") == critic_status and
                      isinstance(acknowledged, bool) and gate_step["status"] == decision and
                      gate_step["output"].get("decision") == expected_message and
                      _valid_execution(gate_step["output"].get("execution")) and
                      gate_step["metrics"] == {"safety": review["metrics"]["safety"]} and
                      run["status"] == decision)
        results = [
            {"step_id": critic_step["id"], "agent_name": "Critic Agent",
             "result": "verified" if critic_valid else "diverged",
             "reason": "" if critic_valid else "Critic 审查结果不一致"},
            {"step_id": gate_step["id"], "agent_name": "Gate Agent",
             "result": "verified" if gate_valid else "diverged",
             "reason": "" if gate_valid else "Gate 决策不一致"},
        ]
        return {"run_id": run_id, "status": "verified" if critic_valid and gate_valid else "diverged",
                "reason": "", "steps": results}

    def review_final(
        self, report_id: str, source: Dict[str, Any], final_text: str, acknowledge_warnings: bool = False
    ) -> Dict[str, Any]:
        final_text = final_text.strip()
        critic_rule = self.store.active_critic_rule()
        run_id = self.store.start_agent_run("final_review", report_id, {
            "critic_rules": {"version": critic_rule["version"], "fingerprint": critic_rule["fingerprint"]},
        })
        started = time.perf_counter()
        try:
            critic_started = time.perf_counter()
            review, critic_execution = self.role_executor.execute("Critic Agent", "review_draft", {
                "source": _final_critic_view(source), "candidate": final_text, "rule": critic_rule,
            })
            has_warnings = bool(review["missing_fact_indexes"] or review["missing_headings"] or review["unsupported_claims"])
            critic_status = "passed" if not review["unsafe"] and not has_warnings else "needs_review"
            self.store.add_agent_step(
                run_id, "Critic Agent", critic_status,
                {"fact_count": len(source.get("facts", [])), "final_characters": len(final_text),
                 "source_sha256": _source_hash(source), "final_sha256": _text_hash(final_text)},
                {"missing_fact_indexes": review["missing_fact_indexes"],
                 "missing_evidence_ids": review["missing_evidence_ids"],
                 "missing_headings": review["missing_headings"], "unsafe": review["unsafe"],
                 "unsupported_claim_lines": [item["line"] for item in review["unsupported_claims"]],
                 "execution": critic_execution},
                review["metrics"], _elapsed_ms(critic_started),
            )
            gate_started = time.perf_counter()
            gate_result, gate_execution = self.role_executor.execute("Gate Agent", "decide", {
                "review": _gate_view(review), "mode": "final",
                "warnings_acknowledged": acknowledge_warnings,
            })
            decision = gate_result["decision"]
            self.store.add_agent_step(
                run_id, "Gate Agent", decision,
                {"critic_status": critic_status, "warnings_acknowledged": acknowledge_warnings,
                 "final_sha256": _text_hash(final_text)},
                {"decision": "阻止敏感信息入库" if decision == "blocked" else
                 "等待用户确认审查提示" if decision == "needs_review" else "允许确认终稿",
                 "execution": gate_execution},
                {"safety": review["metrics"]["safety"]}, _elapsed_ms(gate_started),
            )
            self.store.finish_agent_run(run_id, decision, _elapsed_ms(started))
            return {"run_id": run_id, "decision": decision, "review": review}
        except Exception:
            self.store.finish_agent_run(run_id, "failed", _elapsed_ms(started))
            raise

    def confirm_final(
        self, report_id: str, source: Dict[str, Any], final_text: str,
        acknowledge_warnings: bool = False,
    ) -> Dict[str, Any]:
        final_text = final_text.strip()
        if not final_text:
            raise ValueError("终稿不能为空")
        run_id = self.store.start_final_review(
            report_id, _source_hash(source), _text_hash(final_text), acknowledge_warnings,
        )
        return self._execute_final_confirmation(run_id, final_text, 0, 0)

    def recoverable_final_reviews(self) -> list[Dict[str, Any]]:
        runs = self.store.list_recoverable_agent_runs(
            self.STALE_SECONDS, self.MAX_RECOVERIES, "final_review",
        )
        result = []
        for run in runs:
            report = self.store.get_report(run["report_id"])
            checkpoint = self.store.get_agent_checkpoint(run["id"])
            if (report and report["status"] == "draft" and checkpoint and
                    checkpoint["stage"] in {"prepared", "critic_ready"} and
                    checkpoint["handoff"].get("source_sha256") == _source_hash(report["source"])):
                run["stage"] = checkpoint["stage"]
                run["draft_matches"] = (
                    _text_hash(report["draft"].strip()) == checkpoint["handoff"].get("final_sha256")
                )
                result.append(run)
        return result

    def resume_final_review(self, run_id: str, final_text: str) -> Dict[str, Any]:
        final_text = final_text.strip()
        run = self.store.get_agent_run(run_id)
        checkpoint = self.store.get_agent_checkpoint(run_id)
        if (not run or run["workflow"] != "final_review" or not checkpoint or
                checkpoint["stage"] not in {"prepared", "critic_ready"}):
            raise ValueError("可恢复的终稿审查不存在")
        report = self.store.get_report(run["report_id"])
        if (not report or report["status"] != "draft" or
                checkpoint["handoff"].get("source_sha256") != _source_hash(report["source"]) or
                checkpoint["handoff"].get("final_sha256") != _text_hash(final_text)):
            raise ValueError("终稿或事实来源已变化，不能恢复审查")
        if not self.store.claim_agent_run(
            run_id, self.STALE_SECONDS, self.MAX_RECOVERIES, workflow="final_review",
        ):
            raise ValueError("审查仍在执行、已结束或超过恢复次数上限")
        claimed = self.store.get_agent_run(run_id)
        return self._execute_final_confirmation(
            run_id, final_text, claimed["duration_ms"], claimed["recovery_count"],
        )

    def _execute_final_confirmation(
        self, run_id: str, final_text: str, prior_duration_ms: int, recovery_count: int,
    ) -> Dict[str, Any]:
        started = time.perf_counter()

        def duration_ms() -> int:
            return prior_duration_ms + _elapsed_ms(started)

        try:
            run = self.store.get_agent_run(run_id)
            checkpoint = self.store.get_agent_checkpoint(run_id)
            report = self.store.get_report(run["report_id"])
            source = report["source"]
            handoff = checkpoint["handoff"]
            critic_rule = self.store.critic_rule_for_snapshot(run["instruction_snapshot"])
            if (report["status"] != "draft" or _source_hash(source) != handoff["source_sha256"] or
                    _text_hash(final_text) != handoff["final_sha256"]):
                raise ValueError("审查规则、终稿或事实来源已变化，不能继续")
            critic_started = time.perf_counter()
            review, critic_execution = self.role_executor.execute("Critic Agent", "review_draft", {
                "source": _final_critic_view(source), "candidate": final_text, "rule": critic_rule,
            })
            summary = {
                "unsafe": review["unsafe"],
                "has_warnings": bool(review["missing_fact_indexes"] or review["missing_headings"] or
                                     review["unsupported_claims"]),
                "safety": review["metrics"]["safety"],
            }
            critic_status = "passed" if not summary["unsafe"] and not summary["has_warnings"] else "needs_review"
            critic_input = {
                "fact_count": len(source.get("facts", [])), "final_characters": len(final_text),
                "source_sha256": handoff["source_sha256"], "final_sha256": handoff["final_sha256"],
            }
            critic_output = {
                "missing_fact_indexes": review["missing_fact_indexes"],
                "missing_evidence_ids": review["missing_evidence_ids"],
                "missing_headings": review["missing_headings"], "unsafe": review["unsafe"],
                "unsupported_claim_lines": [item["line"] for item in review["unsupported_claims"]],
                "execution": critic_execution,
            }
            if checkpoint["stage"] == "prepared":
                self.store.add_agent_step(
                    run_id, "Critic Agent", critic_status,
                    critic_input, critic_output,
                    review["metrics"], _elapsed_ms(critic_started),
                    checkpoint={"stage": "critic_ready", "attempt": 0, "candidate_draft": "",
                                "review": summary, "handoff": handoff, "input_hash": handoff["final_sha256"]},
                    expected_recovery_count=recovery_count, cumulative_duration_ms=duration_ms(),
                )
            else:
                stored_step = run["steps"][0] if len(run["steps"]) == 1 else None
                stored_output = stored_step["output"] if stored_step else {}
                functional_output = {key: value for key, value in critic_output.items() if key != "execution"}
                if (checkpoint["stage"] != "critic_ready" or checkpoint["review"] != summary or
                        stored_step is None or stored_step["agent_name"] != "Critic Agent" or
                        stored_step["status"] != critic_status or stored_step["input"] != critic_input or
                        any(stored_output.get(key) != value for key, value in functional_output.items()) or
                        not _valid_execution(stored_output.get("execution")) or
                        stored_step["metrics"] != review["metrics"]):
                    raise ValueError("Critic 检查点与当前审查规则不一致")
            gate_started = time.perf_counter()
            gate_result, gate_execution = self.role_executor.execute("Gate Agent", "decide", {
                "review": _gate_view(review), "mode": "final",
                "warnings_acknowledged": handoff["warnings_acknowledged"],
            })
            decision, confirmed = self.store.complete_final_review(
                run_id, recovery_count, final_text, handoff["source_sha256"],
                duration_ms(), _elapsed_ms(gate_started), gate_result["decision"], gate_execution,
            )
            return {"run_id": run_id, "decision": decision, "review": review, "report": confirmed,
                    "warnings_acknowledged": handoff["warnings_acknowledged"]}
        except ValueError as exc:
            self.store.finish_agent_run(run_id, "invalidated", duration_ms(), type(exc).__name__, recovery_count)
            raise
        except Exception as exc:
            self.store.finish_agent_run(run_id, "failed", duration_ms(), type(exc).__name__, recovery_count)
            raise

    def _replay_strategy_evaluation(self, run: Dict[str, Any]) -> Dict[str, Any]:
        run_id = run["id"]

        def unavailable(reason: str) -> Dict[str, Any]:
            return {"run_id": run_id, "status": "unavailable", "reason": reason, "steps": []}

        snapshot = run["instruction_snapshot"]
        binding = snapshot.get("evaluation")
        if not isinstance(binding, dict) or not all(
            key in binding for key in ("run_id", "candidate_version", "baseline_version",
                                      "benchmark_version", "benchmark_fingerprint")
        ):
            return unavailable("旧运行缺少评测关联信息")
        if (not isinstance(binding["run_id"], str) or
                type(binding["candidate_version"]) is not int or
                type(binding["baseline_version"]) is not int or
                (binding["benchmark_version"] is not None and type(binding["benchmark_version"]) is not int) or
                (binding["benchmark_fingerprint"] is not None and
                 not isinstance(binding["benchmark_fingerprint"], str))):
            return unavailable("评测关联信息已损坏")
        if snapshot.get("evaluation_rules", {}).get("version") != 1:
            return unavailable("评测规则版本不兼容")
        steps = run["steps"]
        if [item["agent_name"] for item in steps] != ["Fact Agent", "Writer Agent", "Critic Agent", "Gate Agent"]:
            return unavailable("旧运行缺少完整评测步骤")
        if any(not all(isinstance(step[key], dict) for key in ("input", "output", "metrics")) for step in steps):
            return {"run_id": run_id, "status": "diverged", "reason": "评测步骤结构无效", "steps": []}
        try:
            evaluation = self.store.get_evaluation_run(binding["run_id"])
        except (ValueError, json.JSONDecodeError):
            return unavailable("评测记录已损坏")
        if evaluation is None:
            return unavailable("关联的评测记录不存在")
        strategies = {item["version"]: item for item in self.store.list_strategies()}
        candidate = strategies.get(binding["candidate_version"])
        baseline = strategies.get(binding["baseline_version"])
        if candidate is None or baseline is None:
            return unavailable("关联的策略版本不存在")
        benchmark = next((item for item in self.store.list_evaluation_benchmarks()
                          if item["version"] == binding["benchmark_version"]), None)
        fact, writer, critic, gate = steps
        metrics = evaluation["metrics"]
        evidence = evaluation["evidence"]
        if not isinstance(evidence, dict) or not isinstance(metrics, dict):
            return {"run_id": run_id, "status": "diverged", "reason": "评测记录结构无效", "steps": []}
        try:
            current_validation = metrics["current_validation"]
            candidate_validation = metrics["candidate_validation"]
            current_holdout = metrics["current_holdout"]
            candidate_holdout = metrics["candidate_holdout"]
            validation_improves = candidate_validation["score"] > current_validation["score"]
            holdout_safe = bool(candidate_holdout and current_holdout and
                                candidate_holdout["score"] >= current_holdout["score"] and
                                candidate_holdout["safety"] >= current_holdout["safety"])
            expected_gate_metrics = {
                "current_validation": current_validation["score"],
                "candidate_validation": candidate_validation["score"],
                "current_holdout": current_holdout["score"] if current_holdout else None,
                "candidate_holdout": candidate_holdout["score"] if candidate_holdout else None,
            }
        except (KeyError, TypeError, ValueError):
            return {"run_id": run_id, "status": "diverged", "reason": "评测指标结构无效", "steps": []}
        benchmark_matches = (
            (binding["benchmark_version"] is None and binding["benchmark_fingerprint"] is None and benchmark is None)
            or (benchmark is not None and benchmark["fingerprint"] == binding["benchmark_fingerprint"])
        )
        validation_count = fact["input"].get("validation_count")
        holdout_count = fact["input"].get("holdout_count")
        validation_ids = evidence.get("validation_report_ids")
        fact_valid = (benchmark_matches and
                      evidence.get("benchmark_version") == binding["benchmark_version"] and
                      evidence.get("benchmark_fingerprint") == binding["benchmark_fingerprint"] and
                      type(validation_count) is int and type(holdout_count) is int and
                      validation_count >= 0 and holdout_count >= 0 and
                      isinstance(validation_ids, list) and validation_count == len(validation_ids) and
                      (benchmark is None or holdout_count == len(benchmark["holdout_ids"])) and
                      (candidate_holdout is None or benchmark is not None and benchmark["holdout_used_at"] is not None) and
                      fact["output"].get("dataset_ready") ==
                      (validation_count >= 3 and holdout_count >= 1) and
                      fact["status"] == "passed")
        writer_valid = (evaluation["candidate_version"] == binding["candidate_version"] and
                        writer["status"] == "passed" and writer["output"].get("candidate_config") == candidate["config"])
        critic_valid = (critic["status"] == "passed" and candidate["metrics"] == metrics and
                        critic["output"].get("candidate_validation") == candidate_validation and
                        critic["output"].get("candidate_holdout") == candidate_holdout and
                        evidence.get("holdout_evaluated") == (candidate_holdout is not None and current_holdout is not None))
        expected_decision = "activated" if (
            benchmark is not None and type(validation_count) is int and validation_count >= 3 and
            validation_improves and candidate["config"] != baseline["config"] and holdout_safe
        ) else "deferred"
        gate_valid = (gate["status"] == expected_decision and run["status"] == expected_decision and
                      evaluation["decision"] == expected_decision and
                      gate["output"].get("reason") == evaluation["reason"] and
                      gate["metrics"] == expected_gate_metrics)
        checks = ((fact, fact_valid, "基准或样本数量不一致"),
                  (writer, writer_valid, "候选策略版本不一致"),
                  (critic, critic_valid, "评测指标不一致"),
                  (gate, gate_valid, "门禁决策不一致"))
        results = [{"step_id": step["id"], "agent_name": step["agent_name"],
                    "result": "verified" if valid else "diverged", "reason": "" if valid else reason}
                   for step, valid, reason in checks]
        return {"run_id": run_id, "status": "verified" if all(item[1] for item in checks) else "diverged",
                "reason": "", "steps": results}

    def record_evaluation(
        self, validation_count: int, holdout_count: int, candidate_config: Dict[str, Any],
        metrics: Dict[str, Any], decision: str, reason: str,
        *, evaluation_run_id: str, candidate_version: int, baseline_version: int,
        benchmark_version: int | None, benchmark_fingerprint: str | None,
    ) -> str:
        run_id = self.store.start_agent_run("strategy_evaluation", instruction_snapshot={
            "evaluation_rules": {"version": 1},
            "evaluation": {"run_id": evaluation_run_id, "candidate_version": candidate_version,
                           "baseline_version": baseline_version, "benchmark_version": benchmark_version,
                           "benchmark_fingerprint": benchmark_fingerprint},
        })
        started = time.perf_counter()
        self.store.add_agent_step(
            run_id, "Fact Agent", "passed",
            {"validation_count": validation_count, "holdout_count": holdout_count},
            {"dataset_ready": validation_count >= 3 and holdout_count >= 1}, {}, _elapsed_ms(started),
        )
        writer_started = time.perf_counter()
        self.store.add_agent_step(
            run_id, "Writer Agent", "passed",
            {"role": "生成候选写作策略"}, {"candidate_config": candidate_config}, {}, _elapsed_ms(writer_started),
        )
        critic_started = time.perf_counter()
        self.store.add_agent_step(
            run_id, "Critic Agent", "passed",
            {"role": "回放评测候选策略"}, {"candidate_validation": metrics["candidate_validation"], "candidate_holdout": metrics["candidate_holdout"]}, {}, _elapsed_ms(critic_started),
        )
        gate_started = time.perf_counter()
        self.store.add_agent_step(
            run_id, "Gate Agent", decision,
            {"role": "检查 Validation 提升与 Holdout 非退化"}, {"reason": reason},
            {"current_validation": metrics["current_validation"]["score"], "candidate_validation": metrics["candidate_validation"]["score"],
             "current_holdout": metrics["current_holdout"]["score"] if metrics["current_holdout"] else None,
             "candidate_holdout": metrics["candidate_holdout"]["score"] if metrics["candidate_holdout"] else None},
            _elapsed_ms(gate_started),
        )
        self.store.finish_agent_run(run_id, decision, _elapsed_ms(started))
        return run_id


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_hash(source: Dict[str, Any]) -> str:
    return _text_hash(json.dumps(source, ensure_ascii=False, sort_keys=True))


def _stale_charge_ms(run: Dict[str, Any], max_attempt_seconds: int) -> int:
    if run["status"] != "running":
        return 0
    updated_at = datetime.fromisoformat(run["updated_at"])
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    elapsed_ms = (datetime.now(timezone.utc) - updated_at).total_seconds() * 1000
    return min(max_attempt_seconds * 1000, max(0, round(elapsed_ms)))


def _input_hash(source: Dict[str, Any], draft: str) -> str:
    payload = json.dumps({"source": source, "draft": draft}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _planner_view(source: Dict[str, Any]) -> Dict[str, Any]:
    origins = source.get("fact_sources", [])
    return {
        "facts": [None] * len(source.get("facts", [])),
        "fact_sources": [{key: origin.get(key) for key in ("source_type", "project_id", "project_name")}
                         for origin in origins],
        "projects": [{key: project.get(key, "") for key in ("id", "name")}
                     for project in source.get("projects", [])],
        "memory_context": {
            "project_notes": [
                {"id": note.get("id"), "project_id": note.get("project_id")}
                for note in source.get("memory_context", {}).get("project_notes", []) if isinstance(note, dict)
            ],
            "project_guidance": {"section_focus": [
                {"project_id": item.get("project_id"), "sections": item.get("sections", []),
                 "source_ids": item.get("source_ids", [])}
                for item in source.get("memory_context", {}).get("project_guidance", {}).get("section_focus", [])
                if isinstance(item, dict)
            ]},
        },
        "manual_problems": "present" if str(source.get("manual_problems", "")).strip() else "",
        "manual_tomorrow": "present" if str(source.get("manual_tomorrow", "")).strip() else "",
    }


def _fact_view(source: Dict[str, Any]) -> Dict[str, Any]:
    return {"facts": [None] * len(source.get("facts", [])),
            "fact_sources": [{key: origin.get(key) for key in
                              ("source_type", "line", "project_id", "commit_hash")}
                             for origin in source.get("fact_sources", [])]}


def _writer_view(source: Dict[str, Any]) -> Dict[str, Any]:
    return {"facts": source.get("facts", [])}


def _critic_view(source: Dict[str, Any], evidence: list[Dict[str, Any]]) -> Dict[str, Any]:
    facts = source.get("facts", [])
    return {"kind": source.get("kind"),
            "facts": [facts[item["fact_index"]] for item in evidence],
            "fact_ids": [item["id"] for item in evidence]}


def _final_critic_view(source: Dict[str, Any]) -> Dict[str, Any]:
    facts = source.get("facts", [])
    return {"kind": source.get("kind"), "facts": facts,
            "fact_ids": [f"F{index + 1:04d}" for index in range(len(facts))]}


def _writer_feedback(review: Dict[str, Any], evidence: list[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    missing_ids = set(review.get("missing_evidence_ids", []))
    task_ids = []
    for item in evidence or []:
        if item["id"] in missing_ids and item["task_id"] not in task_ids:
            task_ids.append(item["task_id"])
    unsupported = [{key: item[key] for key in ("line", "column_start", "column_end")}
                   for item in review.get("unsupported_claims", [])
                   if all(key in item for key in ("line", "column_start", "column_end"))]
    issue_types = []
    for present, name in (
        (review.get("unsafe"), "sensitive_information"),
        (review.get("unsupported_claims"), "unsupported_claims"),
        (review.get("missing_headings"), "missing_headings"),
        (review.get("missing_fact_indexes"), "missing_facts"),
    ):
        if present:
            issue_types.append(name)
    return {
        "missing_fact_indexes": review.get("missing_fact_indexes", []),
        "missing_evidence_ids": review.get("missing_evidence_ids", []),
        "missing_headings": review.get("missing_headings", []),
        "unsupported_claims": unsupported,
        "unsafe": bool(review.get("unsafe")),
        "task_ids": task_ids,
        "issue_types": issue_types,
    }


def _replan_tasks(plan: Dict[str, Any], task_states: Dict[str, str],
                  plan_revisions: list[Dict[str, Any]], feedback: Dict[str, Any],
                  attempt: int) -> Dict[str, Any]:
    _validate_plan(plan, sum(len(task["fact_indexes"]) for task in plan["tasks"]))
    if attempt not in (1, 2) or len(plan_revisions) != attempt - 1:
        raise ValueError("返工计划轮次无效")
    if set(task_states) != {task["id"] for task in plan["tasks"]} or any(
            value != "completed" for value in task_states.values()):
        raise ValueError("返工前必须完成原任务图")
    action_specs = (
        ("redact_sensitive", bool(feedback.get("unsafe")), [], []),
        ("remove_unsupported_claims", bool(feedback.get("unsupported_claims")), [], []),
        ("restore_headings", bool(feedback.get("missing_headings")), [], []),
        ("restore_facts", bool(feedback.get("missing_fact_indexes")),
         list(feedback.get("task_ids", [])), list(feedback.get("missing_evidence_ids", []))),
    )
    nodes = []
    previous_id = ""
    for action, needed, target_task_ids, evidence_ids in action_specs:
        if not needed:
            continue
        node_id = f"R{attempt}-{len(nodes) + 1:03d}"
        node = {"id": node_id, "action": action,
                "depends_on": [previous_id] if previous_id else [],
                "target_task_ids": target_task_ids, "evidence_ids": evidence_ids,
                "status": "planned"}
        nodes.append(node)
        previous_id = node_id
    previous_by_action = {node["action"]: node["id"] for revision in plan_revisions
                          for node in revision["nodes"]}
    revision = {
        "attempt": attempt, "nodes": nodes,
        "execution_order": [node["id"] for node in nodes],
        "superseded_task_ids": [previous_by_action[node["action"]] for node in nodes
                                if node["action"] in previous_by_action],
    }
    _validate_plan_revision(revision, plan, plan_revisions)
    return revision


def _validate_plan_revision(revision: Dict[str, Any], plan: Dict[str, Any],
                            history: list[Dict[str, Any]]) -> None:
    if not isinstance(revision, dict) or set(revision) != {
        "attempt", "nodes", "execution_order", "superseded_task_ids",
    }:
        raise ValueError("返工任务图结构无效")
    attempt, nodes = revision["attempt"], revision["nodes"]
    if attempt not in (1, 2) or attempt != len(history) + 1 or not isinstance(nodes, list):
        raise ValueError("返工任务图轮次无效")
    ids = [node.get("id") for node in nodes]
    if ids != revision["execution_order"] or len(ids) != len(set(ids)):
        raise ValueError("返工任务顺序无效")
    allowed_actions = {"redact_sensitive", "remove_unsupported_claims", "restore_headings", "restore_facts"}
    original_ids = {task["id"] for task in plan["tasks"]}
    known_revision_ids = {node["id"] for item in history for node in item["nodes"]}
    if (not isinstance(revision["superseded_task_ids"], list) or
            any(item not in known_revision_ids for item in revision["superseded_task_ids"])):
        raise ValueError("返工替换任务无效")
    for index, node in enumerate(nodes):
        if set(node) != {"id", "action", "depends_on", "target_task_ids", "evidence_ids", "status"}:
            raise ValueError("返工任务字段无效")
        if (node["action"] not in allowed_actions or node["status"] not in {"planned", "completed"} or
                node["depends_on"] != ([] if index == 0 else [ids[index - 1]]) or
                not isinstance(node["target_task_ids"], list) or
                any(item not in original_ids for item in node["target_task_ids"]) or
                not isinstance(node["evidence_ids"], list) or
                any(not isinstance(item, str) or not re.fullmatch(r"F\d{4}", item)
                    for item in node["evidence_ids"])):
            raise ValueError("返工任务内容无效")


def _gate_view(review: Dict[str, Any]) -> Dict[str, Any]:
    return {"passed": review["passed"], "unsafe": review["unsafe"],
            "missing_fact_count": len(review["missing_fact_indexes"]),
            "missing_heading_count": len(review["missing_headings"]),
            "unsupported_claim_count": len(review["unsupported_claims"])}


def _decide_gate(review: Dict[str, Any], mode: str, warnings_acknowledged: bool) -> Dict[str, str]:
    if mode == "daily":
        return {"decision": "passed" if review["passed"] else "needs_review"}
    if mode != "final":
        raise ValueError("Gate 模式无效")
    has_warnings = bool(review["missing_fact_count"] or review["missing_heading_count"] or
                        review["unsupported_claim_count"])
    decision = ("blocked" if review["unsafe"] else "passed"
                if not has_warnings or warnings_acknowledged else "needs_review")
    return {"decision": decision}


def _valid_execution(value: Any) -> bool:
    return (isinstance(value, dict) and value.get("mode") == "isolated_process" and
            value.get("protocol_version") == ROLE_PROTOCOL_VERSION and type(value.get("worker_pid")) is int and
            value["worker_pid"] > 0 and type(value.get("duration_ms")) is int and
            value["duration_ms"] >= 0)


def _require_role_operation(handoff: Dict[str, Any], role: str, operation: str) -> None:
    allowed = handoff.get("permissions", {}).get(role, {}).get("operations", [])
    if handoff.get("contract_version") != ROLE_CONTRACT_VERSION or operation not in allowed:
        raise ValueError(f"{role} 无权执行 {operation}")


def _daily_handoff(
    source: Dict[str, Any], include_evidence: bool, plan: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    plan = plan or _build_plan(_planner_view(source))
    _validate_plan(plan, len(source.get("facts", [])))
    task_states = {task["id"]: "completed" if include_evidence else "pending" for task in plan["tasks"]}
    return {"contract_version": ROLE_CONTRACT_VERSION, "permissions": ROLE_PERMISSIONS,
            "plan": plan, "task_states": task_states,
            "plan_revisions": [],
            "evidence": _prepare_evidence(_fact_view(source), plan) if include_evidence else []}


def _valid_daily_handoff(source: Dict[str, Any], handoff: Dict[str, Any], stage: str) -> bool:
    expected = _daily_handoff(source, False)
    if any(handoff.get(key) != expected[key] for key in ("contract_version", "permissions", "plan")):
        return False
    states = handoff.get("task_states")
    if not isinstance(states, dict) or set(states) != set(expected["task_states"]):
        return False
    if any(value not in {"pending", "completed"} for value in states.values()):
        return False
    for task in handoff["plan"]["tasks"]:
        if states[task["id"]] == "completed" and any(states[item] != "completed" for item in task["depends_on"]):
            return False
    completed_ids = {task_id for task_id, value in states.items() if value == "completed"}
    simulated = {task_id: "pending" for task_id in states}
    simulated_handoff = {**handoff, "task_states": simulated}
    for _ in range(len(completed_ids)):
        task = _next_ready_task(simulated_handoff)
        if task is None or task["id"] not in completed_ids:
            return False
        simulated[task["id"]] = "completed"
    expected_evidence = [item for item in _prepare_evidence(_fact_view(source), handoff["plan"])
                         if states[item["task_id"]] == "completed"]
    if handoff.get("evidence") != expected_evidence:
        return False
    revisions = handoff.get("plan_revisions")
    if not isinstance(revisions, list) or len(revisions) > 2:
        return False
    history = []
    try:
        for revision in revisions:
            _validate_plan_revision(revision, handoff["plan"], history)
            history.append(revision)
    except (KeyError, TypeError, ValueError):
        return False
    if stage == "replanned":
        if not revisions or any(node["status"] != "planned" for node in revisions[-1]["nodes"]):
            return False
    elif any(node["status"] != "completed" for revision in revisions for node in revision["nodes"]):
        return False
    all_completed = all(value == "completed" for value in states.values())
    return (not all_completed if stage == "planned" and states else True) and (
        stage == "planned" or all_completed)


def _next_ready_task(handoff: Dict[str, Any]) -> Dict[str, Any] | None:
    tasks = {task["id"]: task for task in handoff["plan"]["tasks"]}
    states = handoff["task_states"]
    for task_id in handoff["plan"]["execution_order"]:
        task = tasks[task_id]
        if states[task_id] == "pending" and all(states[item] == "completed" for item in task["depends_on"]):
            return task
    return None


def _build_plan(source: Dict[str, Any]) -> Dict[str, Any]:
    facts = source.get("facts", [])
    origins = source.get("fact_sources", [])
    tasks = []
    assigned = set()
    projects = source.get("projects", [])
    selected_project_ids = {str(project.get("id", "")) for project in projects}
    note_ids_by_project: Dict[str, list[str]] = {}
    for note in source.get("memory_context", {}).get("project_notes", []):
        if not isinstance(note, dict):
            continue
        project_id, note_id = note.get("project_id"), note.get("id")
        if (isinstance(project_id, str) and project_id and project_id in selected_project_ids and
                isinstance(note_id, str) and note_id):
            ids = note_ids_by_project.setdefault(project_id, [])
            if note_id not in ids:
                ids.append(note_id)

    def add_task(scope: str, indexes: list[int], project_id: str = "",
                 depends_on: list[str] | None = None,
                 context_note_ids: list[str] | None = None) -> None:
        action = ("cover_facts" if indexes else "context_only") if scope in {"manual", "project"} else (
            "verify_source" if scope == "unattributed" else "preserve_section")
        task = {"id": f"T{len(tasks) + 1:03d}", "scope": scope, "fact_indexes": indexes,
                "action": action, "depends_on": depends_on or []}
        if project_id:
            task["project_id"] = project_id
            linked_notes = context_note_ids if context_note_ids is not None else note_ids_by_project.get(project_id, [])
            if linked_notes:
                task["context_note_ids"] = linked_notes
        tasks.append(task)
        assigned.update(indexes)

    manual = [index for index in range(len(facts)) if index < len(origins)
              and origins[index].get("source_type") == "manual"]
    if manual:
        add_task("manual", manual)
    for project in sorted(projects, key=lambda item: not bool(note_ids_by_project.get(str(item.get("id", ""))))):
        project_id = str(project.get("id", ""))
        project_name = str(project.get("name", ""))
        indexes = [index for index in range(len(facts)) if index not in assigned and index < len(origins)
                   and origins[index].get("source_type") == "git_commit"
                   and ((project_id and origins[index].get("project_id") == project_id)
                        or (not origins[index].get("project_id") and origins[index].get("project_name") == project_name))]
        add_task("project", indexes, project_id)
    unassigned = [index for index in range(len(facts)) if index not in assigned]
    if unassigned:
        add_task("unattributed", unassigned)
    fact_tasks = [task["id"] for task in tasks if task["fact_indexes"]]
    if str(source.get("manual_problems", "")).strip():
        add_task("problems", [], depends_on=fact_tasks)
    if str(source.get("manual_tomorrow", "")).strip():
        add_task("plans", [], depends_on=[tasks[-1]["id"]] if tasks and tasks[-1]["scope"] == "problems"
                 else fact_tasks)
    focus = source.get("memory_context", {}).get("project_guidance", {}).get("section_focus", [])
    section_focus = []
    for item in focus:
        if not isinstance(item, dict) or item.get("project_id") not in selected_project_ids:
            continue
        sections = [section for section in item.get("sections", [])
                    if section in {"今日完成", "遇到的问题", "明日计划"}]
        source_ids = [source_id for source_id in item.get("source_ids", [])
                      if isinstance(source_id, str) and source_id]
        if sections and source_ids:
            section_focus.append({"project_id": item["project_id"], "sections": sections,
                                  "source_ids": source_ids})
    focus_scopes = {"今日完成": "memory_focus_completed", "遇到的问题": "memory_focus_problems",
                    "明日计划": "memory_focus_plans"}
    for item in section_focus:
        for section in item["sections"]:
            add_task(focus_scopes[section], [], item["project_id"], fact_tasks, item["source_ids"])
    plan = {"tasks": tasks, "execution_order": [task["id"] for task in tasks],
            "required_sections": ["今日完成", "遇到的问题", "明日计划"],
            "section_focus": section_focus}
    _validate_plan(plan, len(facts))
    return plan


def _validate_plan(plan: Dict[str, Any], fact_count: int) -> None:
    tasks = plan.get("tasks")
    order = plan.get("execution_order")
    if not isinstance(tasks, list) or not isinstance(order, list):
        raise ValueError("任务图结构无效")
    task_ids = [task.get("id") for task in tasks]
    if len(set(task_ids)) != len(task_ids) or order != task_ids:
        raise ValueError("任务图编号或执行顺序无效")
    assigned = []
    known = set(task_ids)
    for task in tasks:
        dependencies = task.get("depends_on")
        indexes = task.get("fact_indexes")
        if (not isinstance(dependencies, list) or any(item not in known or item == task["id"] for item in dependencies)
                or not isinstance(indexes, list) or any(type(item) is not int or item < 0 or item >= fact_count for item in indexes)):
            raise ValueError("任务图依赖或事实索引无效")
        assigned.extend(indexes)
    if sorted(assigned) != list(range(fact_count)):
        raise ValueError("任务图必须且只能覆盖每条事实一次")
    section_focus = plan.get("section_focus", [])
    if (not isinstance(section_focus, list) or any(
            not isinstance(item, dict) or set(item) != {"project_id", "sections", "source_ids"} or
            not isinstance(item["project_id"], str) or not item["project_id"] or
            not isinstance(item["sections"], list) or not item["sections"] or
            any(section not in {"今日完成", "遇到的问题", "明日计划"} for section in item["sections"]) or
            not isinstance(item["source_ids"], list) or not item["source_ids"] or
            any(not isinstance(source_id, str) or not source_id for source_id in item["source_ids"])
            for item in section_focus)):
        raise ValueError("任务图中的项目记忆关注章节无效")
    dependencies = {task["id"]: task["depends_on"] for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("任务图存在循环依赖")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in order:
        visit(task_id)


def _prepare_evidence(source: Dict[str, Any], plan: Dict[str, Any]) -> list[Dict[str, Any]]:
    origins = source.get("fact_sources", [])
    task_by_index = {
        index: task["id"] for task in plan["tasks"] for index in task["fact_indexes"]
    }
    items = []
    for index, _ in enumerate(source.get("facts", [])):
        origin = origins[index] if index < len(origins) else {}
        source_type = origin.get("source_type") or "snapshot"
        source_ref = {"line": origin.get("line")} if source_type == "manual" else (
            {"project_id": origin.get("project_id", ""), "commit_hash": origin.get("commit_hash", "")}
            if source_type == "git_commit" else {}
        )
        items.append({"id": f"F{index + 1:04d}", "fact_index": index,
                      "task_id": task_by_index[index], "source_type": source_type,
                      "source_ref": source_ref})
    return items


def _review_with_critic_skill(source: Dict[str, Any], candidate: str,
                              rule: Dict[str, Any]) -> Dict[str, Any]:
    match = rule["skill"]["claim_match"]
    return review_report(
        source, candidate, match["threshold_percent"], match["ngram_overlap_weight_percent"],
        match.get("semantic_weight_percent", 0), match.get("uncertainty_margin_percent", 0),
    )


def _execute_plan_task(source: Dict[str, Any], plan: Dict[str, Any], task: Dict[str, Any]) -> list[Dict[str, Any]]:
    return [item for item in _prepare_evidence(source, plan) if item["task_id"] == task["id"]]


def _prepare_writer_draft(
    draft: str, source: Dict[str, Any], evidence: list[Dict[str, Any]], plan: Dict[str, Any],
) -> str:
    candidate = redact_sensitive(draft)
    normalized = re.sub(r"\s+", "", candidate).lower()
    additions = []
    facts = source.get("facts", [])
    task_order = {task_id: index for index, task_id in enumerate(plan["execution_order"])}
    for item in sorted(evidence, key=lambda fact: task_order[fact["task_id"]]):
        fact = redact_sensitive(str(facts[item["fact_index"]]).strip())
        key = re.sub(r"\s+", "", fact).lower()
        if key and key not in normalized:
            additions.append(f"- {fact}")
            normalized += key
    if not additions:
        return candidate
    return re.sub(r"(?m)^(##[ \t]+今日完成[ \t]*\n)",
                  lambda match: match.group(1) + "\n".join(additions) + "\n", candidate, count=1)


def _revise_draft(draft: str, initial_draft: str, source: Dict[str, Any],
                  feedback: Dict[str, Any], repair_plan: Dict[str, Any], revision: int) -> str:
    candidate = redact_sensitive(initial_draft if revision == 2 else draft)
    actions = [node["action"] for node in repair_plan["nodes"]]
    if "remove_unsupported_claims" in actions:
        candidate = _remove_claim_spans(candidate, feedback.get("unsupported_claims", []))
    if "restore_headings" in actions:
        for heading in feedback.get("missing_headings", []):
            if not re.search(rf"(?m)^##[ \t]+{re.escape(heading)}[ \t]*$", candidate):
                candidate = candidate.rstrip() + f"\n\n## {heading}\n- 暂无已记录内容\n"
    facts = source.get("facts", [])
    additions = []
    for index in feedback.get("missing_fact_indexes", []):
        if index < len(facts):
            fact = redact_sensitive(str(facts[index]).replace("\r", " ").replace("\n", " ").strip())
            if fact and re.sub(r"\s+", "", fact).lower() not in re.sub(r"\s+", "", candidate).lower():
                additions.append(f"- {fact}")
    if additions:
        if "## 今日完成\n" not in candidate:
            candidate = "## 今日完成\n" + "\n".join(additions) + "\n\n" + candidate.lstrip()
        else:
            candidate = candidate.replace("## 今日完成\n", "## 今日完成\n" + "\n".join(additions) + "\n", 1)
    return redact_sensitive(candidate)


def _remove_claim_spans(candidate: str, spans: list[Dict[str, int]]) -> str:
    lines = candidate.splitlines()
    trailing_newline = candidate.endswith("\n")
    by_line: Dict[int, list[Dict[str, int]]] = {}
    for span in spans:
        by_line.setdefault(span["line"], []).append(span)
    for line_number, items in by_line.items():
        if not 1 <= line_number <= len(lines):
            continue
        line = lines[line_number - 1]
        for item in sorted(items, key=lambda value: value["column_start"], reverse=True):
            start, end = item["column_start"], item["column_end"]
            if 0 <= start < end <= len(line):
                line = line[:start] + line[end:]
        line = re.sub(r"[，,、;；。\s]+$", "", line)
        line = re.sub(r"([，,、;；])\s*([，,、;；])", r"\1", line)
        lines[line_number - 1] = "" if re.fullmatch(r"\s*(?:[-*]|\d+[.)、])?\s*", line) else line
    result = "\n".join(line for line in lines if line != "")
    return result + ("\n" if trailing_newline else "")
