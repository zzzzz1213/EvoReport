from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from queue import Empty, Queue
from pathlib import Path
from typing import Any, Dict


ROLE_PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_ROLE_TIMEOUT_SECONDS = 5.0

ROLE_OPERATIONS = {
    "Planner Agent": {"plan_tasks", "replan_tasks"},
    "Fact Agent": {"execute_task"},
    "Writer Agent": {"write_draft", "revise_draft"},
    "Critic Agent": {"review_draft"},
    "Gate Agent": {"decide"},
}


class RoleProcessError(RuntimeError):
    pass


class RoleProcessTimeout(TimeoutError):
    pass


class RoleProcessExecutor:
    """Run allowlisted role operations in dedicated Python processes."""

    def __init__(self, timeout_seconds: float = DEFAULT_ROLE_TIMEOUT_SECONDS):
        if timeout_seconds <= 0:
            raise ValueError("角色执行超时必须大于 0")
        self.timeout_seconds = timeout_seconds
        self.project_root = Path(__file__).resolve().parent.parent
        self._workers: Dict[str, subprocess.Popen[str]] = {}
        self._locks = {role: threading.Lock() for role in ROLE_OPERATIONS}

    @staticmethod
    def worker_environment(source: Dict[str, str] | None = None) -> Dict[str, str]:
        current = source if source is not None else os.environ
        allowed = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP")
        environment = {key: current[key] for key in allowed if current.get(key)}
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        return environment

    def execute(
        self, role: str, operation: str, payload: Dict[str, Any], timeout_seconds: float | None = None,
    ) -> tuple[Any, Dict[str, Any]]:
        if role not in ROLE_OPERATIONS or operation not in ROLE_OPERATIONS[role]:
            raise ValueError(f"{role} 无权执行 {operation}")
        if not isinstance(payload, dict):
            raise TypeError("角色输入必须是对象")
        request = json.dumps({
            "protocol_version": ROLE_PROTOCOL_VERSION,
            "role": role,
            "operation": operation,
            "payload": payload,
        }, ensure_ascii=False, separators=(",", ":"))
        if len(request.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("角色输入超过大小限制")
        timeout = self.timeout_seconds if timeout_seconds is None else min(self.timeout_seconds, timeout_seconds)
        if timeout <= 0:
            raise RoleProcessTimeout(f"{role} 执行前已耗尽时间预算")
        started = time.perf_counter()
        with self._role_lock(role, timeout):
            remaining = timeout - (time.perf_counter() - started)
            if remaining <= 0:
                raise RoleProcessTimeout(f"{role} 等待执行时已耗尽时间预算")
            worker = self._worker(role)
            try:
                assert worker.stdin is not None
                worker.stdin.write(request + "\n")
                worker.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._terminate(role)
                raise RoleProcessError(f"{role} 子进程无法接收任务") from exc
            remaining = timeout - (time.perf_counter() - started)
            if remaining <= 0:
                self._terminate(role)
                raise RoleProcessTimeout(f"{role} 执行超时，子进程已终止")
            response_line = self._readline(role, worker, remaining)
            try:
                response = json.loads(response_line)
            except json.JSONDecodeError as exc:
                self._terminate(role)
                raise RoleProcessError(f"{role} 返回了无效的执行结果") from exc
            if (not isinstance(response, dict) or
                    response.get("protocol_version") != ROLE_PROTOCOL_VERSION or
                    type(response.get("worker_pid")) is not int or
                    response["worker_pid"] != worker.pid or type(response.get("ok")) is not bool):
                self._terminate(role)
                raise RoleProcessError(f"{role} 返回结果不符合角色协议")
            if not response["ok"]:
                code = response.get("error_code", "RoleProcessError")
                message = response.get("message", "角色子进程执行失败")
                raise RoleProcessError(f"{code}: {message}")
            if "output" not in response:
                self._terminate(role)
                raise RoleProcessError(f"{role} 返回结果不符合角色协议")
        duration_ms = round((time.perf_counter() - started) * 1000)
        return response["output"], {
            "mode": "isolated_process",
            "protocol_version": ROLE_PROTOCOL_VERSION,
            "worker_pid": response["worker_pid"],
            "duration_ms": duration_ms,
        }

    @contextmanager
    def _role_lock(self, role: str, timeout: float):
        lock = self._locks[role]
        if not lock.acquire(timeout=timeout):
            raise RoleProcessTimeout(f"{role} 等待执行时已耗尽时间预算")
        try:
            yield
        finally:
            lock.release()

    def _worker(self, role: str) -> subprocess.Popen[str]:
        worker = self._workers.get(role)
        if worker is not None and worker.poll() is not None:
            self._terminate(role)
            worker = None
        if worker is None:
            worker = subprocess.Popen(
                [sys.executable, "-m", "evoreport.role_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.project_root,
                env=self.worker_environment(),
            )
            self._workers[role] = worker
        return worker

    def _readline(self, role: str, worker: subprocess.Popen[str], timeout: float) -> str:
        result: Queue[str | Exception] = Queue(maxsize=1)

        def read() -> None:
            assert worker.stdout is not None
            try:
                result.put(worker.stdout.readline(MAX_RESPONSE_BYTES + 1))
            except (OSError, ValueError) as exc:
                result.put(exc)

        threading.Thread(target=read, daemon=True).start()
        try:
            line = result.get(timeout=timeout)
        except Empty as exc:
            self._terminate(role)
            raise RoleProcessTimeout(f"{role} 执行超过 {timeout:g} 秒，子进程已终止") from exc
        if isinstance(line, Exception):
            self._terminate(role)
            raise RoleProcessError(f"{role} 子进程读取失败") from line
        if not line:
            self._terminate(role)
            raise RoleProcessError(f"{role} 子进程意外退出")
        if len(line.encode("utf-8")) > MAX_RESPONSE_BYTES:
            self._terminate(role)
            raise RoleProcessError(f"{role} 返回结果超过大小限制")
        return line

    def _terminate(self, role: str) -> None:
        worker = self._workers.pop(role, None)
        if worker is None:
            return
        try:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=2)
        finally:
            for stream in (worker.stdin, worker.stdout, worker.stderr):
                if stream is not None:
                    stream.close()

    def close(self) -> None:
        for role, lock in self._locks.items():
            with lock:
                self._terminate(role)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
