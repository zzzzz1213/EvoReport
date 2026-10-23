import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from evoreport.api import create_app
from evoreport.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ARTIFACT_ENV = "EVOREPORT_ARTIFACT_PATH"
_DB_ENV = "EVOREPORT_DATABASE_PATH"


class ArtifactPathIsolationTests(unittest.TestCase):
    """验证 EVOREPORT_ARTIFACT_PATH 的读取与导出/备份落点隔离。"""

    def test_from_env_reads_artifact_path_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict("os.environ", {
                _ARTIFACT_ENV: str(root / "artifacts"),
                _DB_ENV: str(root / "data" / "test.db"),
            }, clear=False):
                settings = Settings.from_env()
        self.assertEqual(settings.artifact_path, (root / "artifacts").resolve())
        self.assertEqual(settings.database_path, (root / "data" / "test.db").resolve())

    def test_from_env_defaults_artifact_path_to_project_root(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.artifact_path, PROJECT_ROOT)
        self.assertEqual(settings.database_path, PROJECT_ROOT / "evoreport.db")

    def test_exports_and_backups_only_land_in_artifact_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "artifacts"
            settings = Settings(
                "127.0.0.1", 8090, root / "data" / "test.db",
                ark_api_key="", artifact_path=artifacts,
            )
            client = TestClient(create_app(settings))

            # 添加普通项目并生成、确认一份日报，供导出使用。
            resp = client.post("/api/projects", json={
                "name": "隔离验证项目", "path": str(root), "project_type": "plain",
            })
            self.assertEqual(201, resp.status_code, resp.text)
            project_id = resp.json()["id"]
            resp = client.post("/api/reports/daily", json={
                "report_date": "2026-10-13", "project_ids": [project_id],
                "completed": "完成隔离验证", "problems": "", "tomorrow": "",
            })
            self.assertEqual(201, resp.status_code, resp.text)
            report_id = resp.json()["id"]
            resp = client.post(f"/api/reports/{report_id}/confirm", json={
                "final_text": "## 今日完成\n- 完成隔离验证\n\n## 遇到的问题\n- 暂无已记录内容\n\n## 明日计划\n- 暂无已记录内容\n",
            })
            self.assertEqual(200, resp.status_code, resp.text)

            # 触发备份与导出（记录项目根目录 exports/backups 的测试前状态）。
            root_exports = PROJECT_ROOT / "exports"
            root_backups = PROJECT_ROOT / "backups"
            before_exports = set(root_exports.rglob("*")) if root_exports.exists() else set()
            before_backups = set(root_backups.rglob("*")) if root_backups.exists() else set()

            resp = client.post("/api/backups")
            self.assertEqual(201, resp.status_code, resp.text)
            for suffix in ("", "/docx"):
                resp = client.get(f"/api/reports/{report_id}/export{suffix}")
                self.assertEqual(200, resp.status_code, resp.text)
            resp = client.get("/api/reports/export/confirmed")
            self.assertEqual(200, resp.status_code, resp.text)

            # 产物只落在隔离 artifact 目录。
            export_files = list((artifacts / "exports").glob("*"))
            backup_files = list((artifacts / "backups").glob("*.db"))
            self.assertTrue(export_files, "未在隔离 exports 目录生成导出文件")
            self.assertTrue(backup_files, "未在隔离 backups 目录生成备份文件")

            # 项目根目录的 exports/backups 不应新增任何文件。
            after_exports = set(root_exports.rglob("*")) if root_exports.exists() else set()
            after_backups = set(root_backups.rglob("*")) if root_backups.exists() else set()
            self.assertEqual(before_exports, after_exports, "项目根目录 exports 被写入测试产物")
            self.assertEqual(before_backups, after_backups, "项目根目录 backups 被写入测试产物")


if __name__ == "__main__":
    unittest.main()
