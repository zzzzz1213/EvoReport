from __future__ import annotations

import re
import hashlib
import json
import time
from statistics import mean
from typing import Any, Callable, Dict, List

from .evaluation import aggregate, review_report, score_report
from .agent_runtime import MultiAgentRuntime
from .agent_instructions import instruction_hash
from .llm import MODEL_CALL_SECONDS, ModelCallTimeout, call_with_deadline
from .report_generator import ReportStrategy, generate_daily, redact_sensitive
from .store import Store
from .writing_profile import apply_profile, build_writing_profile


class EvolutionService:
    MIN_VALIDATION = 3
    MIN_HOLDOUT = 1
    MIN_MONITOR_SAMPLES = 5
    AUTO_ROLLBACK_WINDOW = 5
    AUTO_ROLLBACK_SCORE_DROP = 0.05
    WRITER_PROMPT_BASELINE_SAMPLES = 5
    PROMPT_VALIDATION_CASES = 6
    PROMPT_HOLDOUT_CASES = 2
    PROMPT_EVAL_SECONDS = 240
    PROMPT_EVAL_MAX_CALLS = 18
    PROMPT_EVAL_MAX_RECOVERIES = 2
    PROMPT_EVAL_STALE_SECONDS = 60

    def __init__(self, store: Store, runtime: MultiAgentRuntime | None = None):
        self.store = store
        self.runtime = runtime

    def status(self) -> Dict[str, Any]:
        active = self.store.active_strategy()
        all_validation = self.store.list_evaluation_cases("validation")
        all_holdout_ids = self.store.evaluation_case_ids("holdout")
        benchmarks = self.store.list_evaluation_benchmarks()
        benchmark = benchmarks[0] if benchmarks else None
        validation = self.store.benchmark_cases(benchmark, "validation") if benchmark else all_validation
        used_holdout_ids = {case_id for item in benchmarks for case_id in item["holdout_ids"]}
        used_validation_ids = {case_id for item in benchmarks for case_id in item["validation_ids"]}
        allowed_validation_ids = {item["id"] for item in all_validation if item["id"] not in used_holdout_ids}
        new_validation = len(allowed_validation_ids - set(benchmark["validation_ids"])) if benchmark else len(allowed_validation_ids)
        new_holdout = len([
            case_id for case_id in all_holdout_ids
            if case_id not in used_holdout_ids and case_id not in used_validation_ids
        ])
        can_create = len(allowed_validation_ids) >= self.MIN_VALIDATION and new_holdout >= self.MIN_HOLDOUT and (
            benchmark is None or new_validation >= 1
        )
        runs = self.store.list_evaluation_runs()
        profile = self._writing_profile(validation)
        review = self.store.review_benchmark()
        return {
            "active_strategy": active,
            "strategies": self.store.list_strategies(),
            "post_activation_monitor": self.post_activation_monitor(active),
            "latest_monitor_check": self.store.latest_strategy_monitor_check(),
            "recent_runs": runs,
            "writing_profile": profile,
            "validation_feedback_count": len([
                item for item in self.store.list_writing_feedback()
                if item["report_id"] in {case["report_id"] for case in validation}
            ]),
            "latest_explanation": runs[0] if runs else None,
            "dataset": {
                "validation": len(validation),
                "holdout": len(benchmark["holdout_ids"]) if benchmark else len(all_holdout_ids),
                "required_validation": self.MIN_VALIDATION,
                "required_holdout": self.MIN_HOLDOUT,
            },
            "validation_coverage": self.store.validation_coverage(all_validation),
            "data_targets": {"validation": 30, "holdout": 10, "review_items": 50,
                             "validation_current": len(all_validation), "holdout_current": len(all_holdout_ids),
                             "review_items_current": review["labeled_claims"],
                             "review_scenarios": review["scenario_coverage"]},
            "benchmark": {
                "version": benchmark["version"], "fingerprint": benchmark["fingerprint"],
                "holdout_used": benchmark["holdout_used_at"] is not None,
                "coverage": benchmark["coverage"],
                "new_validation": new_validation, "new_holdout": new_holdout,
                "can_create_next": can_create,
            } if benchmark else {"version": None, "holdout_used": False, "new_validation": new_validation,
                                  "new_holdout": new_holdout, "can_create_next": can_create, "coverage": None},
            "ready": bool(benchmark and benchmark["holdout_used_at"] is None) or (benchmark is None and can_create),
        }

    def post_activation_monitor(self, active: Dict[str, Any] | None = None) -> Dict[str, Any]:
        active = active or self.store.active_strategy()
        activation = self.store.latest_strategy_activation()
        if not activation or activation["active_version"] != active["version"]:
            return {"status": "unavailable", "required_samples": self.MIN_MONITOR_SAMPLES,
                    "sample_count": 0, "report_ids": [], "previous_metrics": None, "active_metrics": None}
        previous = self.store.get_strategy(activation["previous_version"])
        if previous is None:
            return {"status": "unavailable", "required_samples": self.MIN_MONITOR_SAMPLES,
                    "sample_count": 0, "report_ids": [], "previous_metrics": None, "active_metrics": None}
        cases = self.store.list_post_activation_validation_cases(active["version"], activation["created_at"])
        ready = len(cases) >= self.MIN_MONITOR_SAMPLES
        return {
            "status": "ready" if ready else "insufficient",
            "activation_id": activation["id"], "activation_source": activation["source"],
            "activated_at": activation["created_at"],
            "previous_version": previous["version"], "active_version": active["version"],
            "required_samples": self.MIN_MONITOR_SAMPLES, "sample_count": len(cases),
            "report_ids": [case["report_id"] for case in cases],
            "previous_metrics": self._evaluate(cases, previous["config"], use_saved_preferences=True) if ready else None,
            "active_metrics": self._evaluate(cases, active["config"], use_saved_preferences=True) if ready else None,
        }

    def maybe_auto_rollback(self, report_id: str) -> Dict[str, Any] | None:
        activation = self.store.latest_strategy_activation()
        if not activation or activation["source"] != "evaluation":
            return None
        active = self.store.active_strategy()
        if activation["active_version"] != active["version"]:
            return None
        previous = self.store.get_strategy(activation["previous_version"])
        if previous is None:
            return None
        cases = self.store.list_post_activation_validation_cases(active["version"], activation["created_at"])
        if len(cases) < self.AUTO_ROLLBACK_WINDOW * 2 or cases[-1]["report_id"] != report_id:
            return None
        recent = cases[-self.AUTO_ROLLBACK_WINDOW * 2:]
        windows = []
        for start in (0, self.AUTO_ROLLBACK_WINDOW):
            group = recent[start:start + self.AUTO_ROLLBACK_WINDOW]
            windows.append({
                "report_ids": [case["report_id"] for case in group],
                "previous_metrics": self._evaluate(group, previous["config"], use_saved_preferences=True),
                "active_metrics": self._evaluate(group, active["config"], use_saved_preferences=True),
            })
        score_regression = all(
            item["previous_metrics"]["score"] - item["active_metrics"]["score"] >= self.AUTO_ROLLBACK_SCORE_DROP
            for item in windows
        )
        safety_regression = any(
            item["active_metrics"]["safety"] < item["previous_metrics"]["safety"] for item in windows
        )
        decision = "rolled_back" if score_regression or safety_regression else "kept"
        reason = ("最近两组各 5 份 Validation 日报的综合分均下降至少 0.05，已自动回滚。"
                  if score_regression else "Validation 日报安全性下降，已自动回滚。"
                  if safety_regression else "最近两组各 5 份 Validation 日报未同时达到回滚门槛，继续监测。")
        evidence = {
            "previous_version": previous["version"], "active_version": active["version"],
            "score_drop_threshold": self.AUTO_ROLLBACK_SCORE_DROP,
            "windows": windows,
        }
        return self.store.record_strategy_monitor_check(
            activation["id"], active["version"], previous["version"], recent[-1]["id"],
            decision, reason, evidence,
        )

    @staticmethod
    def _writer_prompt_metrics(cases: List[Dict[str, Any]]) -> Dict[str, float]:
        return aggregate(score_report(case["source"], case["initial_draft"], case["expected_text"])
                         for case in cases)

    def writer_prompt_monitor(self) -> Dict[str, Any]:
        active = self.store.active_writer_prompt()
        switch = self.store.latest_writer_prompt_switch()
        empty = {
            "status": "unavailable", "required_baseline_samples": self.WRITER_PROMPT_BASELINE_SAMPLES,
            "required_active_samples": self.AUTO_ROLLBACK_WINDOW * 2,
            "baseline_sample_count": 0, "active_sample_count": 0,
            "baseline_report_ids": [], "active_report_ids": [],
            "baseline_metrics": None, "active_metrics": None,
        }
        if not switch or switch["active_version"] != active["version"]:
            return empty
        baseline = self.store.list_writer_prompt_monitor_cases(
            switch["previous_version"], switch["created_at"], after=False,
            limit=self.WRITER_PROMPT_BASELINE_SAMPLES,
        )
        active_cases = self.store.list_writer_prompt_monitor_cases(
            active["version"], switch["created_at"], after=True,
        )
        ready = (len(baseline) >= self.WRITER_PROMPT_BASELINE_SAMPLES and
                 len(active_cases) >= self.AUTO_ROLLBACK_WINDOW)
        current_window = active_cases[-self.AUTO_ROLLBACK_WINDOW:] if ready else []
        return {
            "status": "ready" if ready else "insufficient",
            "switch_id": switch["id"], "switch_source": switch["source"],
            "switched_at": switch["created_at"],
            "previous_version": switch["previous_version"], "active_version": active["version"],
            "required_baseline_samples": self.WRITER_PROMPT_BASELINE_SAMPLES,
            "required_active_samples": self.AUTO_ROLLBACK_WINDOW * 2,
            "baseline_sample_count": len(baseline), "active_sample_count": len(active_cases),
            "baseline_report_ids": [case["report_id"] for case in baseline],
            "active_report_ids": [case["report_id"] for case in active_cases],
            "baseline_metrics": self._writer_prompt_metrics(baseline) if ready else None,
            "active_metrics": self._writer_prompt_metrics(current_window) if ready else None,
        }

    def maybe_auto_rollback_writer_prompt(self, report_id: str) -> Dict[str, Any] | None:
        switch = self.store.latest_writer_prompt_switch()
        if not switch or switch["source"] != "evaluation":
            return None
        active = self.store.active_writer_prompt()
        if switch["active_version"] != active["version"]:
            return None
        baseline = self.store.list_writer_prompt_monitor_cases(
            switch["previous_version"], switch["created_at"], after=False,
            limit=self.WRITER_PROMPT_BASELINE_SAMPLES,
        )
        active_cases = self.store.list_writer_prompt_monitor_cases(
            active["version"], switch["created_at"], after=True,
        )
        required = self.AUTO_ROLLBACK_WINDOW * 2
        if (len(baseline) < self.WRITER_PROMPT_BASELINE_SAMPLES or len(active_cases) < required or
                active_cases[-1]["report_id"] != report_id):
            return None
        baseline_metrics = self._writer_prompt_metrics(baseline)
        recent = active_cases[-required:]
        windows = []
        for start in (0, self.AUTO_ROLLBACK_WINDOW):
            group = recent[start:start + self.AUTO_ROLLBACK_WINDOW]
            windows.append({
                "report_ids": [case["report_id"] for case in group],
                "metrics": self._writer_prompt_metrics(group),
            })
        score_regression = all(
            baseline_metrics["score"] - item["metrics"]["score"] >= self.AUTO_ROLLBACK_SCORE_DROP
            for item in windows
        )
        safety_regression = any(
            item["metrics"]["safety"] < baseline_metrics["safety"] for item in windows
        )
        decision = "rolled_back" if score_regression or safety_regression else "kept"
        reason = ("新 Writer 指令连续两组各 5 份 Validation 日报的综合分均低于上线前基线至少 0.05，已自动回滚。"
                  if score_regression else "新 Writer 指令的 Validation 日报安全性低于上线前基线，已自动回滚。"
                  if safety_regression else "新 Writer 指令最近两组日报未同时达到回滚门槛，继续监测。")
        evidence = {
            "previous_version": switch["previous_version"], "active_version": active["version"],
            "score_drop_threshold": self.AUTO_ROLLBACK_SCORE_DROP,
            "baseline_report_ids": [case["report_id"] for case in baseline],
            "baseline_metrics": baseline_metrics, "windows": windows,
        }
        return self.store.record_writer_prompt_monitor_check(
            switch["id"], active["version"], switch["previous_version"], recent[-1]["id"],
            decision, reason, evidence,
        )

    def create_benchmark(self) -> Dict[str, Any]:
        return self.store.create_evaluation_benchmark(self.MIN_VALIDATION, self.MIN_HOLDOUT)

    def evaluate_writer_prompt(self, version: int) -> Dict[str, Any]:
        writer = self.runtime.writer if self.runtime else None
        if writer is None or not writer.enabled:
            raise ValueError("模型未配置，不能评测 Writer 指令")
        benchmarks = self.store.list_evaluation_benchmarks()
        if not benchmarks or benchmarks[0]["holdout_used_at"]:
            raise ValueError("需要尚未使用 Holdout 的最新固定基准")
        benchmark = benchmarks[0]
        baseline = self.store.active_writer_prompt()
        candidate = self.store.get_writer_prompt(version)
        if candidate is None or candidate["version"] == baseline["version"]:
            raise ValueError("候选 Writer 指令不存在或已是活动版本")
        for prompt in (baseline, candidate):
            if instruction_hash(prompt["instruction"]) != prompt["sha256"]:
                raise ValueError("Writer 指令版本内容与指纹不一致")
        counts = benchmark.get("prompt_eligible_counts")
        validation_count = min(self.PROMPT_VALIDATION_CASES, counts["validation"]) if counts else 3
        holdout_count = min(self.PROMPT_HOLDOUT_CASES, counts["holdout"]) if counts else 1
        validation = self.store.benchmark_prompt_cases(benchmark, "validation", validation_count)
        if len(validation) < 3:
            raise ValueError("固定基准缺少 3 份真实生成并确认的 Validation 日报")
        if holdout_count < 1:
            raise ValueError("固定基准缺少真实生成并确认的 Holdout 日报")
        evidence: Dict[str, Any] = {
            "benchmark_fingerprint": benchmark["fingerprint"],
            "baseline_sha256": baseline["sha256"], "candidate_sha256": candidate["sha256"],
            "writer_model": getattr(writer, "model", None), "evaluation_rules_version": 2 if counts else 1,
            "validation_count": validation_count, "holdout_count": holdout_count,
            "validation_scenario_coverage": self.store.benchmark_prompt_validation_coverage(benchmark, validation),
            "required_model_calls": 2 * (validation_count + holdout_count),
            "max_model_calls": self.PROMPT_EVAL_MAX_CALLS,
            "validation_case_ids": [case["id"] for case in validation],
            "validation_cases": [], "holdout_cases": [],
        }
        evaluation_id = self.store.start_writer_prompt_evaluation(
            candidate["version"], baseline["version"], benchmark["version"], evidence,
        )
        return self._execute_writer_prompt_evaluation(evaluation_id)

    def recoverable_writer_prompt_evaluations(self) -> List[Dict[str, Any]]:
        rows = self.store.list_recoverable_writer_prompt_evaluations(
            self.PROMPT_EVAL_STALE_SECONDS, self.PROMPT_EVAL_MAX_RECOVERIES,
            self.PROMPT_EVAL_MAX_CALLS, self.PROMPT_EVAL_SECONDS * 1000,
        )
        return [{"id": row["id"], "workflow": "writer_prompt_evaluation",
                 "status": row["decision"], "stage": row["stage"],
                 "candidate_version": row["candidate_version"],
                 "model_calls": row["model_calls"], "max_model_calls": self.PROMPT_EVAL_MAX_CALLS,
                 "duration_ms": row["duration_ms"], "recovery_count": row["recovery_count"]}
                for row in rows]

    def resume_writer_prompt_evaluation(self, evaluation_id: str) -> Dict[str, Any]:
        writer = self.runtime.writer if self.runtime else None
        if writer is None or not writer.enabled:
            raise ValueError("模型未配置，不能恢复 Writer 指令评测")
        claimed = self.store.claim_writer_prompt_evaluation(
            evaluation_id, self.PROMPT_EVAL_STALE_SECONDS, self.PROMPT_EVAL_MAX_RECOVERIES,
            self.PROMPT_EVAL_MAX_CALLS, self.PROMPT_EVAL_SECONDS * 1000,
        )
        if claimed is None:
            raise ValueError("评测仍在执行、已结束或超过恢复预算")
        return self._execute_writer_prompt_evaluation(evaluation_id)

    def _execute_writer_prompt_evaluation(self, evaluation_id: str) -> Dict[str, Any]:
        evaluation = self.store.get_writer_prompt_evaluation(evaluation_id)
        started = time.monotonic()
        prior_duration_ms = evaluation["duration_ms"]
        recovery_count = evaluation["recovery_count"]
        progress = evaluation["progress"]
        metrics: Dict[str, Any] = {"validation": None, "holdout": None}
        evidence = evaluation["evidence"]

        def duration_ms() -> int:
            return prior_duration_ms + max(0, round((time.monotonic() - started) * 1000))

        try:
            benchmarks = self.store.list_evaluation_benchmarks()
            benchmark = benchmarks[0] if benchmarks else None
            baseline = self.store.get_writer_prompt(evaluation["baseline_version"])
            candidate = self.store.get_writer_prompt(evaluation["candidate_version"])
            if (benchmark is None or benchmark["version"] != evaluation["benchmark_version"] or
                    benchmark["fingerprint"] != evidence["benchmark_fingerprint"] or
                    baseline is None or candidate is None or
                    evidence.get("evaluation_rules_version") not in (1, 2) or
                    evidence.get("writer_model") != getattr(self.runtime.writer, "model", None) or
                    self.store.active_writer_prompt()["version"] != baseline["version"] or
                    any(instruction_hash(prompt["instruction"]) != evidence[f"{label}_sha256"] or
                        prompt["sha256"] != evidence[f"{label}_sha256"]
                        for label, prompt in (("baseline", baseline), ("candidate", candidate)))):
                raise ValueError("固定基准或 Writer 指令版本已变化")
            writer = self.runtime.writer
            validation_count = evidence.get("validation_count", 3)
            holdout_count = evidence.get("holdout_count", 1)
            validation = self.store.benchmark_prompt_cases(
                benchmark, "validation", validation_count,
            )
            if ([case["id"] for case in validation] != evidence["validation_case_ids"] or
                    len(validation) != validation_count):
                raise ValueError("Validation 样本快照已变化")
            validation_result = self._compare_writer_prompts_checkpointed(
                evaluation_id, recovery_count, "validation", validation,
                baseline, candidate, writer, progress, duration_ms,
            )
            metrics["validation"] = validation_result["metrics"]
            evidence["validation_cases"] = validation_result["cases"]
            if (validation_result["metrics"]["candidate"]["score"] <=
                    validation_result["metrics"]["baseline"]["score"] or
                    not validation_result["candidate_reviews_passed"]):
                decision, reason = "failed", "Validation 综合分未提升，或候选输出未通过 Critic；未读取 Holdout。"
            else:
                if evaluation["stage"] == "validation":
                    holdout = self.store.claim_benchmark_prompt_holdout(
                        benchmark, holdout_count, evaluation_id, recovery_count,
                    )
                else:
                    if benchmark["holdout_run_id"] != evaluation_id:
                        raise ValueError("Holdout 门禁不属于当前评测")
                    holdout = self.store.benchmark_prompt_cases(
                        benchmark, "holdout", holdout_count,
                    )
                if holdout is None:
                    decision, reason = "deferred", "固定基准的 Holdout 已由其他评测领取，本次未读取 Holdout。"
                else:
                    holdout_result = self._compare_writer_prompts_checkpointed(
                        evaluation_id, recovery_count, "holdout_claimed", holdout,
                        baseline, candidate, writer, progress, duration_ms,
                    )
                    metrics["holdout"] = holdout_result["metrics"]
                    evidence["holdout_cases"] = holdout_result["cases"]
                    candidate_metrics = holdout_result["metrics"]["candidate"]
                    baseline_metrics = holdout_result["metrics"]["baseline"]
                    passed = (holdout_result["candidate_reviews_passed"] and
                              candidate_metrics["score"] >= baseline_metrics["score"] and
                              candidate_metrics["safety"] >= baseline_metrics["safety"])
                    decision = "passed" if passed else "failed"
                    reason = ("Validation 提升，Holdout 综合分与安全性未退化，候选输出通过 Critic。" if passed
                              else "Holdout 综合分或安全性退化，或候选输出未通过 Critic。")
        except ValueError as exc:
            return self.store.interrupt_writer_prompt_evaluation(
                evaluation_id, recovery_count, f"基准样本核验或评测预算失败：{exc}",
                metrics, evidence, duration_ms(), False,
            )
        except Exception as exc:
            current = self.store.get_writer_prompt_evaluation(evaluation_id)
            recoverable = ((not isinstance(exc, TimeoutError) or isinstance(exc, ModelCallTimeout)) and
                           current["model_calls"] < self.PROMPT_EVAL_MAX_CALLS and
                           duration_ms() < self.PROMPT_EVAL_SECONDS * 1000)
            return self.store.interrupt_writer_prompt_evaluation(
                evaluation_id, recovery_count,
                f"模型评测中断：{type(exc).__name__}；已完成的样本结果保留。",
                metrics, evidence, duration_ms(), recoverable,
            )
        try:
            return self.store.finish_writer_prompt_evaluation(
                evaluation_id, decision, reason, metrics, evidence, recovery_count, duration_ms(),
            )
        except ValueError:
            raise
        except Exception as exc:
            return self.store.interrupt_writer_prompt_evaluation(
                evaluation_id, recovery_count, f"评测结果提交中断：{type(exc).__name__}。",
                metrics, evidence, duration_ms(), True,
            )

    def _compare_writer_prompts_checkpointed(
        self, evaluation_id: str, recovery_count: int, stage: str,
        cases: List[Dict[str, Any]], baseline: Dict[str, Any], candidate: Dict[str, Any],
        writer: Any, progress: Dict[str, Any], duration_ms: Callable[[], int],
    ) -> Dict[str, Any]:
        key = "validation_cases" if stage == "validation" else "holdout_cases"
        evidence = progress[key]
        for case in cases:
            draft = redact_sensitive(case["initial_draft"])
            item = next((entry for entry in evidence if entry["case_id"] == case["id"]), None)
            if item is None:
                item = {"case_id": case["id"], "report_id": case["report_id"],
                        "input_sha256": instruction_hash(draft)}
                evidence.append(item)
            elif item["report_id"] != case["report_id"] or item["input_sha256"] != instruction_hash(draft):
                raise ValueError("评测样本输入与检查点不一致")
            for label, prompt in (("baseline", baseline), ("candidate", candidate)):
                if label in item:
                    continue
                if duration_ms() >= self.PROMPT_EVAL_SECONDS * 1000:
                    raise TimeoutError("Writer 指令评测超过总时间预算")
                self.store.reserve_writer_prompt_call(
                    evaluation_id, recovery_count, self.PROMPT_EVAL_MAX_CALLS,
                    self.PROMPT_EVAL_SECONDS * 1000,
                )
                try:
                    remaining = self.PROMPT_EVAL_SECONDS - duration_ms() / 1000
                    output, used_model, _ = call_with_deadline(
                        lambda draft=draft, instruction=prompt["instruction"]: writer.rewrite(
                            draft, instruction=instruction),
                        min(MODEL_CALL_SECONDS, remaining),
                    )
                except ModelCallTimeout:
                    raise
                except Exception as exc:
                    raise RuntimeError("模型调用未完成") from exc
                if duration_ms() >= self.PROMPT_EVAL_SECONDS * 1000:
                    raise TimeoutError("Writer 指令评测超过总时间预算")
                if not used_model:
                    raise RuntimeError("模型未返回可用于评测的输出")
                review = review_report(case["source"], output)
                score = score_report(case["source"], output, case["expected_text"])
                item[label] = {
                    "score": score, "critic_passed": review["passed"],
                    "missing_facts": len(review["missing_fact_indexes"]),
                    "missing_headings": len(review["missing_headings"]),
                    "unsupported_claims": len(review["unsupported_claims"]), "unsafe": review["unsafe"],
                }
                self.store.save_writer_prompt_progress(
                    evaluation_id, recovery_count, stage, progress, duration_ms(),
                )
        scores = {label: [item[label]["score"] for item in evidence] for label in ("baseline", "candidate")}
        candidate_reviews_passed = all(item["candidate"]["critic_passed"] for item in evidence)
        return {"metrics": {label: aggregate(values) for label, values in scores.items()},
                "cases": evidence, "candidate_reviews_passed": candidate_reviews_passed}

    def propose(self) -> Dict[str, Any]:
        benchmarks = self.store.list_evaluation_benchmarks()
        benchmark = benchmarks[0] if benchmarks else None
        if benchmark is None:
            try:
                benchmark = self.create_benchmark()
            except ValueError:
                pass
        validation = self.store.benchmark_cases(benchmark, "validation") if benchmark else self.store.list_evaluation_cases("validation")
        active = self.store.active_strategy()
        profile = self._writing_profile(validation)
        candidate_config = apply_profile(self._derive_candidate(validation, active["config"]), profile)
        validation_ids = {case["report_id"] for case in validation}
        feedback = [
            item for item in self.store.list_writing_feedback("validation")
            if item["report_id"] in validation_ids and item["signals"]["changed"]
        ]
        style_feedback = [
            item for item in feedback
            if item["signals"]["list_style_before"] != item["signals"]["list_style_after"]
            and item["signals"]["list_style_after"] in {"numbered", "bulleted"}
        ]
        if style_feedback:
            numbered_votes = sum(item["signals"]["list_style_after"] == "numbered" for item in style_feedback)
            candidate_config["numbered"] = numbered_votes * 2 >= len(style_feedback)
        failures = [
            item for item in self.store.list_validation_critic_failures(active["version"])
            if item["report_id"] in validation_ids
        ]
        missing_fact_failures = [item for item in failures if item["review"].get("missing_fact_indexes")]
        if missing_fact_failures:
            candidate_config["max_item_length"] = min(160, max(
                candidate_config["max_item_length"], active["config"]["max_item_length"] + 20
            ))
        evidence = {
            "benchmark_version": benchmark["version"] if benchmark else None,
            "benchmark_fingerprint": benchmark["fingerprint"] if benchmark else None,
            "validation_report_ids": sorted(validation_ids),
            "feedback_report_ids": sorted(item["report_id"] for item in feedback),
            "critic_run_ids": sorted({item["run_id"] for item in failures}),
            "rules": [
                {"key": key, "before": active["config"][key], "after": candidate_config[key],
                 "source": "终稿列表修改" if key == "numbered" and style_feedback else
                 "审查缺失事实" if key == "max_item_length" and missing_fact_failures else "Validation 终稿画像"}
                for key in active["config"] if candidate_config[key] != active["config"][key]
            ],
        }
        snapshot = {
            "baseline_version": active["version"], "baseline_config": active["config"],
            "candidate_config": candidate_config,
            "benchmark_version": benchmark["version"] if benchmark else None,
            "benchmark_fingerprint": benchmark["fingerprint"] if benchmark else None,
            "validation_case_ids": sorted(item["id"] for item in validation),
            "validation_fingerprint": _validation_fingerprint(validation),
            "validation_count": len(validation),
            "holdout_count": len(benchmark["holdout_ids"]) if benchmark else 0,
        }
        run_id = self.store.start_strategy_evaluation(snapshot, evidence)
        return self._execute_strategy_evaluation(run_id, 0, 0)

    def recoverable_proposals(self) -> List[Dict[str, Any]]:
        runs = self.store.list_recoverable_agent_runs(60, 2, "strategy_evaluation")
        result = []
        for run in runs:
            checkpoint = self.store.get_strategy_evaluation_checkpoint(run["id"])
            if checkpoint and checkpoint["stage"] in {
                "prepared", "validation_ready", "holdout_claimed", "decision_ready",
            }:
                run["stage"] = checkpoint["stage"]
                result.append(run)
        return result

    def resume_proposal(self, run_id: str) -> Dict[str, Any]:
        run = self.store.get_agent_run(run_id)
        checkpoint = self.store.get_strategy_evaluation_checkpoint(run_id)
        if (not run or run["workflow"] != "strategy_evaluation" or not checkpoint or
                checkpoint["stage"] not in {"prepared", "validation_ready", "holdout_claimed", "decision_ready"}):
            raise ValueError("可恢复的格式策略评测不存在")
        if not self.store.claim_agent_run(run_id, 60, 2, workflow="strategy_evaluation"):
            raise ValueError("评测仍在执行、已结束或超过恢复次数上限")
        claimed = self.store.get_agent_run(run_id)
        return self._execute_strategy_evaluation(run_id, claimed["duration_ms"], claimed["recovery_count"])

    def _execute_strategy_evaluation(
        self, run_id: str, prior_duration_ms: int, recovery_count: int,
    ) -> Dict[str, Any]:
        started = time.perf_counter()

        def duration_ms() -> int:
            return prior_duration_ms + max(0, round((time.perf_counter() - started) * 1000))

        try:
            checkpoint = self.store.get_strategy_evaluation_checkpoint(run_id)
            snapshot = checkpoint["snapshot"]
            metrics = checkpoint["metrics"]
            evidence = checkpoint["evidence"]
            stage = checkpoint["stage"]
            run = self.store.get_agent_run(run_id)
            if run["instruction_snapshot"].get("evaluation_rules", {}).get("version") != 1:
                raise ValueError("评测规则版本已变化，不能恢复")
            baseline = self.store.get_strategy(snapshot["baseline_version"])
            if (baseline is None or baseline["config"] != snapshot["baseline_config"] or
                    self.store.active_strategy()["version"] != snapshot["baseline_version"]):
                raise ValueError("活动基线策略已变化，不能恢复评测")
            benchmarks = self.store.list_evaluation_benchmarks()
            benchmark = next((item for item in benchmarks
                              if item["version"] == snapshot["benchmark_version"]), None)
            if snapshot["benchmark_version"] is not None:
                if benchmark is None or benchmark["fingerprint"] != snapshot["benchmark_fingerprint"]:
                    raise ValueError("固定基准已变化，不能恢复评测")
                validation = self.store.benchmark_cases(benchmark, "validation")
            else:
                validation = self.store.list_evaluation_cases("validation")
            if (sorted(item["id"] for item in validation) != snapshot["validation_case_ids"] or
                    _validation_fingerprint(validation) != snapshot["validation_fingerprint"]):
                raise ValueError("Validation 快照已变化，不能恢复评测")
            if stage == "prepared":
                metrics["current_validation"] = self._evaluate(validation, snapshot["baseline_config"])
                metrics["candidate_validation"] = self._evaluate(validation, snapshot["candidate_config"])
                self.store.update_strategy_evaluation_checkpoint(
                    run_id, recovery_count, "prepared", "validation_ready", metrics, evidence, duration_ms(),
                )
                stage = "validation_ready"
            if stage == "validation_ready":
                validation_passed = (
                    benchmark is not None and snapshot["validation_count"] >= self.MIN_VALIDATION and
                    metrics["candidate_validation"]["score"] > metrics["current_validation"]["score"] and
                    snapshot["candidate_config"] != snapshot["baseline_config"]
                )
                if validation_passed and self.store.claim_strategy_evaluation_holdout(run_id, recovery_count):
                    stage = "holdout_claimed"
                else:
                    evidence["holdout_evaluated"] = False
                    self.store.update_strategy_evaluation_checkpoint(
                        run_id, recovery_count, "validation_ready", "decision_ready",
                        metrics, evidence, duration_ms(),
                    )
                    stage = "decision_ready"
            if stage == "holdout_claimed":
                claimed = next((item for item in self.store.list_evaluation_benchmarks()
                                if item["version"] == snapshot["benchmark_version"]), None)
                if claimed is None or claimed["holdout_run_id"] != run_id:
                    raise ValueError("Holdout 门禁不属于当前评测运行")
                holdout = self.store.benchmark_cases(benchmark, "holdout")
                metrics["current_holdout"] = self._evaluate(holdout, snapshot["baseline_config"])
                metrics["candidate_holdout"] = self._evaluate(holdout, snapshot["candidate_config"])
                evidence["holdout_evaluated"] = True
                self.store.update_strategy_evaluation_checkpoint(
                    run_id, recovery_count, "holdout_claimed", "decision_ready",
                    metrics, evidence, duration_ms(),
                )
                stage = "decision_ready"
            if stage != "decision_ready":
                raise ValueError("评测检查点阶段无效")
            current_holdout = metrics["current_holdout"]
            candidate_holdout = metrics["candidate_holdout"]
            safe = bool(current_holdout and candidate_holdout and
                        candidate_holdout["score"] >= current_holdout["score"] and
                        candidate_holdout["safety"] >= current_holdout["safety"])
            validation_passed = (
                benchmark is not None and snapshot["validation_count"] >= self.MIN_VALIDATION and
                metrics["candidate_validation"]["score"] > metrics["current_validation"]["score"] and
                snapshot["candidate_config"] != snapshot["baseline_config"]
            )
            decision = "activated" if validation_passed and safe else "deferred"
            if benchmark is None:
                reason = (f"样本不足：Validation {snapshot['validation_count']}/{self.MIN_VALIDATION}，"
                          "Holdout 需要全新样本。")
            elif snapshot["candidate_config"] == snapshot["baseline_config"]:
                reason = "候选策略与当前策略一致，暂无可验证的格式变化。"
            elif not (metrics["candidate_validation"]["score"] > metrics["current_validation"]["score"]):
                reason = "Validation 综合分未提升：{:.4f} → {:.4f}；未访问 Holdout。".format(
                    metrics["current_validation"]["score"], metrics["candidate_validation"]["score"],
                )
            elif candidate_holdout is None:
                reason = f"基准 v{benchmark['version']} 的 Holdout 已使用或基准已更新；请检查当前版本。"
            else:
                reason = self._decision_reason(
                    True, snapshot["validation_count"], snapshot["holdout_count"],
                    snapshot["baseline_config"], snapshot["candidate_config"],
                    metrics["current_validation"], metrics["candidate_validation"],
                    current_holdout, candidate_holdout, decision,
                )
            saved = self.store.finalize_strategy_evaluation(
                run_id, recovery_count, decision, reason, duration_ms(),
            )
            return {"decision": decision, "reason": reason, "candidate": saved["candidate"],
                    "metrics": metrics, "evidence": evidence,
                    "evaluation_run_id": saved["evaluation_run_id"], "trace_run_id": run_id}
        except ValueError as exc:
            self.store.finish_agent_run(
                run_id, "invalidated", duration_ms(), type(exc).__name__, recovery_count,
            )
            raise
        except Exception as exc:
            self.store.finish_agent_run(
                run_id, "failed", duration_ms(), type(exc).__name__, recovery_count,
            )
            raise

    def _writing_profile(self, cases: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        reports = [
            {"final_text": case["expected_text"]}
            for case in (cases if cases is not None else self.store.list_evaluation_cases("validation"))
            if case["source"].get("kind") == "daily"
        ]
        return build_writing_profile(reports)

    @classmethod
    def _decision_reason(
        cls, ready: bool, validation_count: int, holdout_count: int, current: Dict[str, Any], candidate: Dict[str, Any],
        current_validation: Dict[str, float], candidate_validation: Dict[str, float],
        current_holdout: Dict[str, float], candidate_holdout: Dict[str, float], decision: str,
    ) -> str:
        if decision == "activated":
            return "已通过：Validation 综合分提升，且 Holdout 安全性与综合分均未退化。"
        if not ready:
            return f"样本不足：Validation {validation_count}/{cls.MIN_VALIDATION}，Holdout {holdout_count}/{cls.MIN_HOLDOUT}。"
        if candidate == current:
            return "候选策略与当前策略一致，暂无可验证的格式变化。"
        if candidate_validation["score"] <= current_validation["score"]:
            return "Validation 综合分未提升：{:.4f} → {:.4f}。".format(
                current_validation["score"], candidate_validation["score"]
            )
        if candidate_holdout["safety"] < current_holdout["safety"]:
            return "Holdout 安全性下降：{:.4f} → {:.4f}。".format(
                current_holdout["safety"], candidate_holdout["safety"]
            )
        return "Holdout 综合分下降：{:.4f} → {:.4f}。".format(
            current_holdout["score"], candidate_holdout["score"]
        )

    def _evaluate(
        self, cases: List[Dict[str, Any]], config: Dict[str, Any], use_saved_preferences: bool = False,
    ) -> Dict[str, float]:
        scores = []
        for case in cases:
            source = case["source"]
            if source.get("kind") != "daily":
                continue
            preferences = source.get("memory_context", {}).get("preferences", {}) if use_saved_preferences else {}
            strategy = ReportStrategy(version=0, **{**config, **preferences})
            generated = generate_daily(
                _parse_date(source["date"]),
                source.get("projects", []),
                source.get("manual_completed", ""),
                source.get("manual_problems", ""),
                source.get("manual_tomorrow", ""),
                strategy,
            )
            scores.append(score_report(source, generated, case["expected_text"]))
        return aggregate(scores)

    @staticmethod
    def _derive_candidate(cases: List[Dict[str, Any]], current: Dict[str, Any]) -> Dict[str, Any]:
        expected = [case["expected_text"] for case in cases if case["source"].get("kind") == "daily"]
        if not expected:
            return dict(current)
        numbered_ratios = []
        item_lengths = []
        statistics_mentions = 0
        for text in expected:
            item_lines = [line.strip() for line in text.splitlines() if re.match(r"^(?:\d+\.|-)\s+", line.strip())]
            if item_lines:
                numbered_ratios.append(sum(1 for line in item_lines if re.match(r"^\d+\.", line)) / len(item_lines))
                item_lengths.extend(len(re.sub(r"^(?:\d+\.|-)\s+", "", line)) for line in item_lines)
            if "开发统计" in text:
                statistics_mentions += 1
        return {
            "numbered": mean(numbered_ratios) >= 0.5 if numbered_ratios else bool(current.get("numbered", True)),
            "include_statistics": statistics_mentions >= len(expected) / 2,
            "max_item_length": max(40, min(160, round(mean(item_lengths)) + 20)) if item_lengths else int(current.get("max_item_length", 100)),
        }


def _parse_date(value: str):
    from datetime import date

    return date.fromisoformat(value)


def _validation_fingerprint(cases: List[Dict[str, Any]]) -> str:
    payload = [{"id": item["id"], "source": item["source"], "expected_text": item["expected_text"]}
               for item in sorted(cases, key=lambda case: case["id"])]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
