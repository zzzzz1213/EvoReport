from __future__ import annotations

import re
from collections import Counter
from statistics import mean
from typing import Any, Dict, Iterable, List


def build_writing_profile(reports: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    texts = [str(item.get("final_text") or item.get("draft") or "") for item in reports]
    texts = [text for text in texts if text.strip()]
    items = []
    numbered = 0
    bullets = 0
    heading_counts = Counter()
    statistics_mentions = 0
    terms = Counter()
    for text in texts:
        if "开发统计" in text:
            statistics_mentions += 1
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                heading_counts[stripped[3:].strip()] += 1
            if re.match(r"^\d+\.\s+", stripped):
                numbered += 1
                items.append(re.sub(r"^\d+\.\s+", "", stripped))
            elif re.match(r"^[-*]\s+", stripped):
                bullets += 1
                items.append(re.sub(r"^[-*]\s+", "", stripped))
        for term in re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]{1,}(?![A-Za-z0-9])", text):
            terms[term] += 1
    total_list_items = numbered + bullets
    average_item_length = round(mean(len(item) for item in items)) if items else 0
    return {
        "sample_count": len(texts),
        "ready": len(texts) >= 4,
        "preferred_list": "编号列表" if numbered >= bullets else "项目符号",
        "list_confidence": round(max(numbered, bullets) / total_list_items, 2) if total_list_items else 0.0,
        "average_item_length": average_item_length,
        "statistics_preference": "保留" if statistics_mentions * 2 >= len(texts) and texts else "不强调",
        "section_coverage": {
            title: round(heading_counts[title] / len(texts), 2) if texts else 0.0
            for title in ("今日完成", "遇到的问题", "明日计划")
        },
        "project_terms": [term for term, _ in terms.most_common(5)],
    }


def apply_profile(candidate: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    if not profile.get("ready"):
        return dict(candidate)
    updated = dict(candidate)
    if profile.get("list_confidence", 0) >= 0.6:
        updated["numbered"] = profile.get("preferred_list") == "编号列表"
    updated["include_statistics"] = profile.get("statistics_preference") == "保留"
    average_length = int(profile.get("average_item_length", 0))
    if average_length:
        updated["max_item_length"] = max(40, min(160, average_length + 20))
    return updated
