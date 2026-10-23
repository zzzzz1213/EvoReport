from __future__ import annotations

import re
from typing import Dict, List


PLAN_MARKERS = ("明日", "明天", "后续", "计划", "待处理")
PROBLEM_MARKERS = ("问题", "异常", "报错", "失败", "缺失", "阻塞", "不一致")
COMPLETED_MARKERS = ("完成", "修复", "优化", "调整", "联调", "测试", "上线", "开发", "排查", "实现", "整理")
SECTION_LABELS = (
    ("completed", re.compile(r"^(?:今日完成|今天完成|今日工作|完成事项)\s*[:：]?\s*")),
    ("problems", re.compile(r"^(?:遇到的问题|遇到问题|存在的问题|问题)\s*(?:是|[:：])\s*")),
    ("tomorrow", re.compile(r"^(?:明日计划|明天计划|明日|明天|后续计划|后续|计划|待处理)\s*[:：]?\s*")),
)


def parse_work_summary(content: str) -> Dict[str, str]:
    sections: Dict[str, List[str]] = {"completed": [], "problems": [], "tomorrow": []}
    fragments = [item.strip(" -：:") for item in re.split(r"[\n。；;]+", content.replace("\r", "")) if item.strip()]
    for fragment in fragments:
        labeled = next(((section, pattern.match(fragment)) for section, pattern in SECTION_LABELS if pattern.match(fragment)), None)
        if labeled:
            section, match = labeled
            value = fragment[match.end():].strip(" -：:")
            if value:
                sections[section].append(value)
            continue
        if any(marker in fragment for marker in PLAN_MARKERS):
            value = re.sub(r".*?(?:明日计划|明天计划|明日|明天|后续计划|计划|待处理)\s*[:：]?\s*", "", fragment).strip()
            if value:
                sections["tomorrow"].append(value)
        elif any(marker in fragment for marker in COMPLETED_MARKERS):
            sections["completed"].append(fragment)
        elif any(marker in fragment for marker in PROBLEM_MARKERS):
            sections["problems"].append(fragment)
        else:
            sections["completed"].append(fragment)
    return {key: "\n".join(_deduplicate(values)) for key, values in sections.items()}


def _deduplicate(items: List[str]) -> List[str]:
    result = []
    seen = set()
    for item in items:
        key = re.sub(r"\s+", "", item)
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result
