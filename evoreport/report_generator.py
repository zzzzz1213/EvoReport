from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Sequence


SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


@dataclass(frozen=True)
class ReportStrategy:
    version: int = 1
    numbered: bool = True
    include_statistics: bool = True
    max_item_length: int = 100

    def as_dict(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "numbered": self.numbered,
            "include_statistics": self.include_statistics,
            "max_item_length": self.max_item_length,
        }


def redact_sensitive(value: str) -> str:
    result = value
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(lambda match: match.group(1) + "=[REDACTED]" if match.lastindex else "[REDACTED]", result)
    return result


def generate_daily(
    target_date: date,
    projects: Sequence[Dict[str, object]],
    manual_completed: str = "",
    manual_problems: str = "",
    manual_tomorrow: str = "",
    strategy: ReportStrategy | None = None,
) -> str:
    active = strategy or ReportStrategy()
    completed = _clean_manual_items(manual_completed)
    for project in projects:
        project_name = str(project.get("name", "项目"))
        activity = project.get("activity", {})
        for commit in activity.get("commits", []):
            subject = str(commit.get("subject", "")).strip()
            if subject:
                completed.append(f"{project_name}：{subject}")
    completed = _deduplicate(completed)
    problems = _clean_manual_items(manual_problems)
    tomorrow = _clean_manual_items(manual_tomorrow)
    sections = [
        f"# 工作日报 · {target_date.isoformat()}",
        _section("今日完成", completed, active),
    ]
    if active.include_statistics and projects:
        stats = []
        for project in projects:
            summary = project.get("activity", {}).get("summary", {})
            if project.get("activity", {}).get("source") == "manual":
                continue
            stats.append(
                f"{project.get('name', '项目')}：{summary.get('commit_count', 0)} 次提交，"
                f"{summary.get('files_changed', 0)} 个文件，+{summary.get('insertions', 0)} "
                f"/-{summary.get('deletions', 0)} 行"
            )
        if stats:
            sections.append(_section("开发统计", stats, active))
    sections.extend([
        _section("遇到的问题", problems, active),
        _section("明日计划", tomorrow, active),
    ])
    return redact_sensitive("\n\n".join(sections).strip() + "\n")


def generate_weekly(week_label: str, daily_reports: Sequence[str], strategy: ReportStrategy | None = None) -> str:
    active = strategy or ReportStrategy()
    completed: List[str] = []
    problems: List[str] = []
    plans: List[str] = []
    for report in daily_reports:
        sections = _extract_sections(report)
        completed.extend(sections.get("今日完成", []))
        problems.extend(sections.get("遇到的问题", []))
        plans.extend(sections.get("明日计划", []))
    return redact_sensitive("\n\n".join([
        f"# 工作周报 · {week_label}",
        _section("本周完成", _deduplicate(completed), active),
        _section("问题与解决", _deduplicate(problems), active),
        _section("下周计划", _deduplicate(plans), active),
    ]).strip() + "\n")


def source_facts(projects: Sequence[Dict[str, object]], manual_completed: str) -> List[str]:
    return [item["fact"] for item in source_fact_records(projects, manual_completed)]


def source_fact_records(projects: Sequence[Dict[str, object]], manual_completed: str) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    seen = set()

    def add(fact: str, provenance: Dict[str, object]) -> None:
        key = re.sub(r"\s+", "", fact).lower()
        if key and key not in seen:
            seen.add(key)
            records.append({"fact": fact, **provenance})

    for line_number, line in enumerate(manual_completed.replace("\r", "").split("\n"), 1):
        for fact in _clean_manual_items(line):
            add(fact, {"source_type": "manual", "line": line_number})
    for project in projects:
        for commit in project.get("activity", {}).get("commits", []):
            subject = str(commit.get("subject", "")).strip()
            if subject:
                add(subject, {
                    "source_type": "git_commit", "project_id": str(project.get("id", "")),
                    "project_name": str(project.get("name", "项目")),
                    "commit_hash": str(commit.get("hash", "")),
                })
    return records


def _section(title: str, items: Iterable[str], strategy: ReportStrategy) -> str:
    cleaned = [redact_sensitive(item.strip())[: strategy.max_item_length] for item in items if item.strip()]
    if not cleaned:
        cleaned = ["暂无已记录内容"]
    lines = []
    for index, item in enumerate(cleaned, 1):
        prefix = f"{index}. " if strategy.numbered else "- "
        lines.append(prefix + item)
    return "## " + title + "\n" + "\n".join(lines)


def _clean_manual_items(value: str) -> List[str]:
    result = []
    for line in value.replace("\r", "").split("\n"):
        cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
        if cleaned:
            result.append(cleaned)
    return result


def _deduplicate(items: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        key = re.sub(r"\s+", "", item).lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _extract_sections(report: str) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    current = ""
    for line in report.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            result.setdefault(current, [])
            continue
        if not current:
            continue
        value = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
        if value and value != "暂无已记录内容":
            result[current].append(value)
    return result
