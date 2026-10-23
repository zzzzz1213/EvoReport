from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

from .role_executor import MAX_REQUEST_BYTES, ROLE_OPERATIONS, ROLE_PROTOCOL_VERSION


PAYLOAD_FIELDS = {
    ("Planner Agent", "plan_tasks"): {"source"},
    ("Planner Agent", "replan_tasks"): {"plan", "task_states", "plan_revisions", "feedback", "attempt"},
    ("Fact Agent", "execute_task"): {"source", "plan", "task"},
    ("Writer Agent", "write_draft"): {"draft", "source", "evidence", "plan"},
    ("Writer Agent", "revise_draft"): {"draft", "initial_draft", "source", "feedback", "revision",
                                         "repair_plan"},
    ("Critic Agent", "review_draft"): {"source", "candidate", "rule"},
    ("Gate Agent", "decide"): {"review", "mode", "warnings_acknowledged"},
}


def _require_object(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是对象")
    return value


def _validate_request(request: Any) -> tuple[str, str, Dict[str, Any]]:
    request = _require_object(request, "请求")
    if set(request) != {"protocol_version", "role", "operation", "payload"}:
        raise ValueError("角色请求字段不符合协议")
    if request["protocol_version"] != ROLE_PROTOCOL_VERSION:
        raise ValueError("角色协议版本不兼容")
    role = request["role"]
    operation = request["operation"]
    if role not in ROLE_OPERATIONS or operation not in ROLE_OPERATIONS[role]:
        raise ValueError("角色操作不在白名单中")
    payload = _require_object(request["payload"], "payload")
    if set(payload) != PAYLOAD_FIELDS[(role, operation)]:
        raise ValueError("角色输入字段不符合协议")
    _validate_payload(role, operation, payload)
    return role, operation, payload


def _validate_payload(role: str, operation: str, payload: Dict[str, Any]) -> None:
    for key in ("source", "plan", "task", "feedback", "review", "rule", "task_states", "repair_plan"):
        if key in payload:
            _require_object(payload[key], key)
    for key in ("draft", "candidate"):
        if key in payload and (not isinstance(payload[key], str) or len(payload[key]) > 500_000):
            raise ValueError(f"{key} 必须是长度受限的文本")
    if "evidence" in payload and (not isinstance(payload["evidence"], list) or len(payload["evidence"]) > 10_000):
        raise ValueError("evidence 必须是长度受限的数组")
    if "plan_revisions" in payload and (not isinstance(payload["plan_revisions"], list) or
                                         len(payload["plan_revisions"]) > 2):
        raise ValueError("plan_revisions 必须是长度受限的数组")
    if "revision" in payload and (type(payload["revision"]) is not int or payload["revision"] not in (1, 2)):
        raise ValueError("revision 只能是 1 或 2")
    if role == "Gate Agent":
        if payload["mode"] not in {"daily", "final"} or type(payload["warnings_acknowledged"]) is not bool:
            raise ValueError("Gate 输入无效")


def _dispatch(role: str, operation: str, payload: Dict[str, Any]) -> Any:
    from .agent_runtime import (_build_plan, _decide_gate, _execute_plan_task, _replan_tasks,
                                _prepare_writer_draft, _review_with_critic_skill, _revise_draft)

    if (role, operation) == ("Planner Agent", "plan_tasks"):
        return _build_plan(payload["source"])
    if (role, operation) == ("Planner Agent", "replan_tasks"):
        return _replan_tasks(payload["plan"], payload["task_states"], payload["plan_revisions"],
                             payload["feedback"], payload["attempt"])
    if (role, operation) == ("Fact Agent", "execute_task"):
        return _execute_plan_task(payload["source"], payload["plan"], payload["task"])
    if (role, operation) == ("Writer Agent", "write_draft"):
        return _prepare_writer_draft(payload["draft"], payload["source"], payload["evidence"], payload["plan"])
    if (role, operation) == ("Writer Agent", "revise_draft"):
        return _revise_draft(payload["draft"], payload["initial_draft"], payload["source"],
                             payload["feedback"], payload["repair_plan"], payload["revision"])
    if (role, operation) == ("Critic Agent", "review_draft"):
        return _review_with_critic_skill(payload["source"], payload["candidate"], payload["rule"])
    if (role, operation) == ("Gate Agent", "decide"):
        return _decide_gate(payload["review"], payload["mode"], payload["warnings_acknowledged"])
    raise ValueError("角色操作没有实现")


def _respond(raw: bytes) -> Dict[str, Any]:
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("角色请求超过大小限制")
        role, operation, payload = _validate_request(json.loads(raw.decode("utf-8")))
        return {"ok": True, "protocol_version": ROLE_PROTOCOL_VERSION,
                "worker_pid": os.getpid(), "output": _dispatch(role, operation, payload)}
    except Exception as exc:
        return {"ok": False, "protocol_version": ROLE_PROTOCOL_VERSION,
                "worker_pid": os.getpid(), "error_code": type(exc).__name__, "message": str(exc)}


def main() -> int:
    while True:
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            return 0
        response = _respond(raw.rstrip(b"\r\n"))
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        if len(raw) > MAX_REQUEST_BYTES:
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
