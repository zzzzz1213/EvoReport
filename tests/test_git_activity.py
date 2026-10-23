import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from evoreport.git_activity import GitActivityError, collect_activity, validate_repository_path


class GitActivityTests(unittest.TestCase):
    def test_non_git_directory_has_chinese_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(GitActivityError, "该目录不是 Git 项目"):
                validate_repository_path(directory)

    def test_collects_today_commit_and_working_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self._git(path, "init")
            self._git(path, "config", "user.name", "EvoReport Test")
            self._git(path, "config", "user.email", "test@example.invalid")
            (path / "demo.txt").write_text("first\n", encoding="utf-8")
            self._git(path, "add", "demo.txt")
            self._git(path, "commit", "-m", "add demo file")
            (path / "demo.txt").write_text("first\nsecond\n", encoding="utf-8")

            activity = collect_activity(str(path), date.today())

            self.assertEqual(1, activity["summary"]["commit_count"])
            self.assertEqual("add demo file", activity["commits"][0]["subject"])
            self.assertEqual(1, activity["summary"]["uncommitted_count"])

    @staticmethod
    def _git(path: Path, *arguments: str) -> None:
        completed = subprocess.run(
            ["git", "-C", str(path), *arguments], capture_output=True, text=True, encoding="utf-8", env=os.environ
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)


if __name__ == "__main__":
    unittest.main()
