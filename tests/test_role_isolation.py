import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from evoreport.agent_runtime import MultiAgentRuntime
from evoreport.role_executor import (ROLE_PROTOCOL_VERSION, RoleProcessError,
                                     RoleProcessExecutor, RoleProcessTimeout)
from evoreport.store import Store


class Writer:
    def rewrite(self, draft):
        return draft, False, "本地写作"


class RoleIsolationTests(unittest.TestCase):
    def test_roles_use_distinct_child_processes_and_trace_execution(self):
        source = {"kind": "daily", "facts": ["完成接口联调"], "projects": []}
        draft = "## 今日完成\n- 完成接口联调\n## 遇到的问题\n## 明日计划\n"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "roles.db")
            report = store.create_report("daily", "2026-10-17", source, draft, 1)
            executor = RoleProcessExecutor()
            result = MultiAgentRuntime(store, Writer(), executor).run_report(report["id"], source, draft)
            steps = store.get_agent_run(result["run_id"])["steps"]
            executor.close()
        role_pids = {}
        for step in steps:
            execution = step["output"]["execution"]
            self.assertEqual("isolated_process", execution["mode"])
            self.assertEqual(ROLE_PROTOCOL_VERSION, execution["protocol_version"])
            self.assertNotEqual(os.getpid(), execution["worker_pid"])
            self.assertIsInstance(execution["duration_ms"], int)
            self.assertGreaterEqual(execution["duration_ms"], 0)
            role_pids.setdefault(step["agent_name"], execution["worker_pid"])
            self.assertEqual(role_pids[step["agent_name"]], execution["worker_pid"])
        self.assertEqual(len(role_pids), len(set(role_pids.values())))

    def test_worker_environment_does_not_inherit_credentials_or_database_path(self):
        environment = RoleProcessExecutor.worker_environment({
            "PATH": "demo-path", "SYSTEMROOT": "demo-root", "ARK_API_KEY": "secret",
            "EVOREPORT_DATABASE_PATH": "private.db", "MODEL_ENDPOINT": "https://example.invalid",
        })
        self.assertEqual("demo-path", environment["PATH"])
        self.assertNotIn("ARK_API_KEY", environment)
        self.assertNotIn("EVOREPORT_DATABASE_PATH", environment)
        self.assertNotIn("MODEL_ENDPOINT", environment)

    def test_operation_and_payload_allowlists_reject_unexpected_access(self):
        executor = RoleProcessExecutor()
        self.addCleanup(executor.close)
        with self.assertRaises(ValueError):
            executor.execute("Critic Agent", "write_draft", {})
        with self.assertRaises(RoleProcessError):
            executor.execute("Planner Agent", "plan_tasks", {"source": {"facts": []}, "database_path": "x"})

    def test_timeout_terminates_worker(self):
        executor = RoleProcessExecutor(timeout_seconds=0.000001)
        self.addCleanup(executor.close)
        with self.assertRaises(RoleProcessTimeout):
            executor.execute("Planner Agent", "plan_tasks", {"source": {"facts": []}})
        self.assertNotIn("Planner Agent", executor._workers)
        executor.timeout_seconds = 5
        output, execution = executor.execute("Planner Agent", "plan_tasks", {"source": {"facts": []}})
        self.assertIsInstance(output, dict)
        self.assertEqual(executor._workers["Planner Agent"].pid, execution["worker_pid"])

    def test_lock_wait_timeout_does_not_terminate_active_worker(self):
        executor = RoleProcessExecutor()
        self.addCleanup(executor.close)
        payload = {"source": {"facts": []}}
        executor.execute("Planner Agent", "plan_tasks", payload)
        worker = executor._workers["Planner Agent"]
        with executor._locks["Planner Agent"]:
            with self.assertRaises(RoleProcessTimeout):
                executor.execute("Planner Agent", "plan_tasks", payload, timeout_seconds=0.02)
        self.assertIsNone(worker.poll())
        self.assertIs(worker, executor._workers["Planner Agent"])

    def test_exited_worker_is_reaped_and_replaced(self):
        executor = RoleProcessExecutor()
        self.addCleanup(executor.close)
        payload = {"source": {"facts": []}}
        executor.execute("Planner Agent", "plan_tasks", payload)
        old = executor._workers["Planner Agent"]
        old.kill()
        old.wait(timeout=2)
        _, execution = executor.execute("Planner Agent", "plan_tasks", payload)
        self.assertNotEqual(old.pid, execution["worker_pid"])
        self.assertTrue(old.stdin.closed)
        self.assertTrue(old.stdout.closed)

    def test_invalid_response_discards_worker_before_next_request(self):
        executor = RoleProcessExecutor()
        self.addCleanup(executor.close)
        payload = {"source": {"facts": []}}
        with patch.object(executor, "_readline", return_value="invalid json"):
            with self.assertRaises(RoleProcessError):
                executor.execute("Planner Agent", "plan_tasks", payload)
        self.assertNotIn("Planner Agent", executor._workers)
        _, execution = executor.execute("Planner Agent", "plan_tasks", payload)
        self.assertEqual(executor._workers["Planner Agent"].pid, execution["worker_pid"])

    def test_response_from_wrong_worker_is_rejected(self):
        executor = RoleProcessExecutor()
        self.addCleanup(executor.close)
        response = '{"ok":true,"protocol_version":%d,"worker_pid":-1,"output":{}}' % ROLE_PROTOCOL_VERSION
        with patch.object(executor, "_readline", return_value=response):
            with self.assertRaises(RoleProcessError):
                executor.execute("Planner Agent", "plan_tasks", {"source": {"facts": []}})
        self.assertNotIn("Planner Agent", executor._workers)

    def test_close_waits_for_active_role_and_releases_streams(self):
        executor = RoleProcessExecutor()
        self.addCleanup(executor.close)
        payload = {"source": {"facts": []}}
        executor.execute("Planner Agent", "plan_tasks", payload)
        worker = executor._workers["Planner Agent"]
        started = threading.Event()
        closed = threading.Event()

        def close():
            started.set()
            executor.close()
            closed.set()

        with executor._locks["Planner Agent"]:
            thread = threading.Thread(target=close)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(closed.wait(0.05))
        thread.join(timeout=3)
        self.assertTrue(closed.is_set())
        self.assertIsNotNone(worker.poll())
        self.assertTrue(worker.stdin.closed)
        self.assertTrue(worker.stdout.closed)


if __name__ == "__main__":
    unittest.main()
