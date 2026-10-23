from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List


class GitActivityError(ValueError):
    pass


@dataclass(frozen=True)
class GitRunner:
    timeout_seconds: int = 15

    def run(self, repository_path: str, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", repository_path, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "Git command failed"
            raise GitActivityError(message)
        return completed.stdout


def validate_repository_path(value: str, runner: GitRunner | None = None) -> Path:
    path = validate_directory_path(value)
    active_runner = runner or GitRunner()
    try:
        result = active_runner.run(str(path), "rev-parse", "--is-inside-work-tree").strip()
    except GitActivityError as exc:
        if "not a git repository" in str(exc).lower():
            raise GitActivityError("该目录不是 Git 项目，请选择包含 .git 的项目根目录") from exc
        raise
    if result != "true":
        raise GitActivityError("该目录不是 Git 项目，请选择包含 .git 的项目根目录")
    return path


def validate_directory_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise GitActivityError("项目目录不存在或不是文件夹")
    return path


def collect_activity(repository_path: str, target_date: date, runner: GitRunner | None = None) -> Dict[str, object]:
    active_runner = runner or GitRunner()
    path = validate_repository_path(repository_path, active_runner)
    start = datetime.combine(target_date, time.min).astimezone()
    end = start + timedelta(days=1)
    log_output = active_runner.run(
        str(path),
        "log",
        "--no-merges",
        "--since=" + start.isoformat(),
        "--until=" + end.isoformat(),
        "--date=iso-strict",
        "--pretty=format:__EVOREPORT_COMMIT__%n%H%n%ad%n%s",
        "--numstat",
    )
    branch = active_runner.run(str(path), "branch", "--show-current").strip() or "detached"
    status_output = active_runner.run(str(path), "status", "--short", "--untracked-files=normal")
    commits = _parse_log(log_output)
    working_tree = []
    for line in status_output.splitlines():
        if not line.strip():
            continue
        working_tree.append({"status": line[:2], "path": line[3:].strip()})
    return {
        "source": "git",
        "path": str(path),
        "date": target_date.isoformat(),
        "branch": branch,
        "commits": commits,
        "working_tree": working_tree,
        "summary": {
            "commit_count": len(commits),
            "files_changed": len({item["path"] for commit in commits for item in commit["files"]}),
            "insertions": sum(item["insertions"] for commit in commits for item in commit["files"]),
            "deletions": sum(item["deletions"] for commit in commits for item in commit["files"]),
            "uncommitted_count": len(working_tree),
        },
    }


def _parse_log(value: str) -> List[Dict[str, object]]:
    commits: List[Dict[str, object]] = []
    current: Dict[str, object] | None = None
    metadata: List[str] = []
    for line in value.splitlines():
        if line == "__EVOREPORT_COMMIT__":
            if current is not None:
                commits.append(current)
            current = {"hash": "", "date": "", "subject": "", "files": []}
            metadata = []
            continue
        if current is None:
            continue
        if len(metadata) < 3:
            metadata.append(line)
            keys = ("hash", "date", "subject")
            current[keys[len(metadata) - 1]] = line.strip()
            continue
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        insertions = int(parts[0]) if parts[0].isdigit() else 0
        deletions = int(parts[1]) if parts[1].isdigit() else 0
        current["files"].append({
            "path": parts[2],
            "insertions": insertions,
            "deletions": deletions,
        })
    if current is not None:
        commits.append(current)
    return commits
