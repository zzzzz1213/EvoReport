from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List

from .report_generator import redact_sensitive


CONSTRAINT_CUES = re.compile(r"需要|必须|兼容|依赖|限制|约束")
MANUAL_FIELDS = ("manual_completed", "manual_problems", "manual_tomorrow")
SYNONYMS = (("API", "接口"), ("api", "接口"), ("必须", "需要"), ("应当", "需要"),
            ("务必", "需要"), ("兼容", "支持"), ("旧版", "旧版本"))


def normalized_memory_text(value: str) -> str:
    result = value.strip()
    for source, target in SYNONYMS:
        result = result.replace(source, target)
    return re.sub(r"[\s，。；;、,.：:（）()]+", "", result).lower()


def memory_similarity(left: str, right: str) -> float:
    first, second = normalized_memory_text(left), normalized_memory_text(right)
    if not first or not second:
        return 0.0
    if first in second or second in first:
        return min(len(first), len(second)) / max(len(first), len(second))
    return SequenceMatcher(None, first, second).ratio()


def memories_conflict(left: str, right: str) -> bool:
    negative = re.compile(r"不再|不得|禁止|无需|不需要|取消")
    left_negative, right_negative = bool(negative.search(left)), bool(negative.search(right))
    if left_negative == right_negative:
        return False
    left_base = normalized_memory_text(negative.sub("", left))
    right_base = normalized_memory_text(negative.sub("", right))
    return SequenceMatcher(None, left_base, right_base).ratio() >= 0.72


def suggest_project_memories(cases: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: List[Dict[str, Any]] = []
    for case in cases:
        source = case["source"]
        projects = source.get("projects") or []
        if len(projects) != 1 or not isinstance(projects[0], dict) or not projects[0].get("id"):
            continue
        project_id = str(projects[0]["id"])
        for field in MANUAL_FIELDS:
            for line_number, raw_line in enumerate(str(source.get(field) or "").splitlines(), 1):
                value = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", raw_line).strip()
                value = redact_sensitive(value).rstrip("。；;，, ")
                if not 6 <= len(value) <= 120 or not CONSTRAINT_CUES.search(value) or "[REDACTED]" in value:
                    continue
                normalized = normalized_memory_text(value)
                item = next((candidate for candidate in grouped
                             if candidate["project_id"] == project_id and
                             not memories_conflict(candidate["value"], value) and
                             memory_similarity(candidate["value"], value) >= 0.84), None)
                if item is None:
                    item = {"scope": "project", "project_id": project_id, "memory_key": "note",
                            "value": value, "normalized": normalized, "evidence": []}
                    grouped.append(item)
                item["evidence"].append({
                    "report_id": case["report_id"], "report_date": case["report_date"],
                    "field": field, "line": line_number,
                })
    suggestions = []
    for item in grouped:
        evidence = list({entry["report_id"]: entry for entry in item["evidence"]}.values())
        if len({entry["report_date"] for entry in evidence}) < 2:
            continue
        project_id, normalized = item["project_id"], item.pop("normalized")
        suggestion_id = hashlib.sha256(f"{project_id}\n{normalized}".encode("utf-8")).hexdigest()
        suggestions.append({"id": suggestion_id, **item, "evidence": evidence,
                            "reason": "不同日期的 Validation 日报重复出现相近项目约束"})
    return sorted(suggestions, key=lambda item: (item["project_id"], item["value"]))


def suggest_personal_memories(feedback: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for item in feedback:
        signals = item.get("signals") or {}
        changes: List[tuple[str, str]] = []
        before, after = signals.get("list_style_before"), signals.get("list_style_after")
        if before != after and after in {"numbered", "bulleted"}:
            changes.append(("numbered", "true" if after == "numbered" else "false"))
        before, after = signals.get("statistics_before"), signals.get("statistics_after")
        if isinstance(before, bool) and isinstance(after, bool) and before != after:
            changes.append(("include_statistics", "true" if after else "false"))
        before, after = signals.get("max_item_length_before"), signals.get("max_item_length_after")
        if type(before) is int and type(after) is int and after > 0 and abs(after - before) >= 10:
            rounded = max(40, min(160, int(round(after / 10) * 10)))
            changes.append(("max_item_length", str(rounded)))
        for memory_key, value in changes:
            key = (memory_key, value)
            candidate = grouped.setdefault(key, {
                "scope": "personal", "project_id": None, "memory_key": memory_key,
                "value": value, "evidence": [],
            })
            candidate["evidence"].append({
                "report_id": item["report_id"], "report_date": item.get("report_date"),
                "before": signals.get({"numbered": "list_style_before",
                                        "include_statistics": "statistics_before",
                                        "max_item_length": "max_item_length_before"}[memory_key]),
                "after": signals.get({"numbered": "list_style_after",
                                       "include_statistics": "statistics_after",
                                       "max_item_length": "max_item_length_after"}[memory_key]),
            })
    suggestions = []
    for (memory_key, value), item in grouped.items():
        evidence = list({entry["report_id"]: entry for entry in item["evidence"]}.values())
        dates = {entry["report_date"] for entry in evidence if entry.get("report_date")}
        if len(evidence) < 2 or len(dates) < 2:
            continue
        suggestion_id = hashlib.sha256(f"personal\n{memory_key}\n{value}".encode("utf-8")).hexdigest()
        suggestions.append({"id": suggestion_id, **item, "evidence": evidence,
                            "reason": "不同日期的 Validation 终稿重复出现相同修改偏好"})
    return sorted(suggestions, key=lambda item: (item["memory_key"], item["value"]))
