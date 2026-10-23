from __future__ import annotations

import re
from difflib import SequenceMatcher
from statistics import mean
from typing import Any, Dict, Iterable, List

from .report_generator import SECRET_PATTERNS, redact_sensitive


REQUIRED_HEADINGS = ("今日完成", "遇到的问题", "明日计划")
WEEKLY_HEADINGS = ("本周完成", "问题与解决", "下周计划")
ACTION_VERBS = (
    r"完成|修复|上线|发布|部署|新增|实现|交付|解决|提交|合并|验证|测试|"
    r"优化|开发|编写|排查|处理|更新|搭建|接入|联调|迁移|重构"
)
CLAIM_SEPARATORS = re.compile(
    r"[;；。]+|[,，]\s*(?:并且|同时)\s*|"
    r"(?:[,，、]\s*(?:(?:并且|同时|并|且|和|以及)\s*)?|(?:并且|同时|并|且|和|以及)\s*)"
    rf"(?=(?:已)?(?:{ACTION_VERBS}))"
)
ACTION_START = re.compile(rf"^(?:已)?(?:{ACTION_VERBS})")
ACTION_ANYWHERE = re.compile(rf"(?:已经|已)?(?P<action>{ACTION_VERBS})")
ACTION_FAMILIES = {
    "完成": "delivery", "实现": "delivery", "交付": "delivery",
    "修复": "repair", "解决": "repair", "处理": "repair",
    "上线": "release", "发布": "release", "部署": "release",
    "验证": "quality", "测试": "quality", "联调": "quality",
    "新增": "build", "开发": "build", "编写": "build", "搭建": "build", "接入": "build",
    "优化": "improve", "重构": "improve", "更新": "improve", "迁移": "improve",
    "排查": "investigate", "提交": "change", "合并": "change",
}
SEMANTIC_ALIASES = (
    (re.compile(r"(?i)api"), "接口"),
    (re.compile(r"(?i)e2e"), "端到端"),
    (re.compile(r"(?i)bug"), "问题"),
    (re.compile(r"缺陷|异常"), "问题"),
    (re.compile(r"说明书|使用说明"), "文档"),
    (re.compile(r"回归验证"), "回归测试"),
    (re.compile(r"正常登录"), "登录成功"),
    (re.compile(r"失败尝试"), "登录失败"),
    (re.compile(r"登录功能"), "认证模块"),
    (re.compile(r"本地发布服务"), "本地服务"),
    (re.compile(r"两个应用版本|不同版本的应用"), "多版本应用"),
)
CRITIC_MATCH_THRESHOLD_PERCENT = 60
CRITIC_MATCH_ALGORITHM_VERSION = 1


def review_report(source: Dict[str, Any], candidate: str,
                  claim_threshold_percent: int = CRITIC_MATCH_THRESHOLD_PERCENT,
                  ngram_overlap_weight_percent: int = 0,
                  semantic_weight_percent: int = 0,
                  uncertainty_margin_percent: int = 0) -> Dict[str, Any]:
    """Review a draft against its source without seeing the Writer's reference draft."""
    if (isinstance(claim_threshold_percent, bool) or not isinstance(claim_threshold_percent, int) or
            not 40 <= claim_threshold_percent <= 80):
        raise ValueError("无来源匹配阈值须为 40% 至 80% 的整数")
    if (isinstance(ngram_overlap_weight_percent, bool) or
            not isinstance(ngram_overlap_weight_percent, int) or
            not 0 <= ngram_overlap_weight_percent <= 50):
        raise ValueError("字符重合度权重须为 0% 至 50% 的整数")
    if (isinstance(semantic_weight_percent, bool) or not isinstance(semantic_weight_percent, int) or
            not 0 <= semantic_weight_percent <= 60):
        raise ValueError("结构化语义权重须为 0% 至 60% 的整数")
    if (isinstance(uncertainty_margin_percent, bool) or
            not isinstance(uncertainty_margin_percent, int) or
            not 0 <= uncertainty_margin_percent <= 20):
        raise ValueError("待核实区间须为 0% 至 20% 的整数")
    facts = [redact_sensitive(str(item).strip()) for item in source.get("facts", []) if str(item).strip()]
    fact_match_scores = [round(_contains_fact(candidate, fact), 4) for fact in facts]
    missing_facts = [index for index, score in enumerate(fact_match_scores) if score < 0.8]
    fact_ids = source.get("fact_ids")
    if not isinstance(fact_ids, list) or len(fact_ids) != len(facts):
        fact_ids = [f"F{index + 1:04d}" for index in range(len(facts))]
    headings = WEEKLY_HEADINGS if source.get("kind") == "weekly" else REQUIRED_HEADINGS
    present_headings = {item.strip() for item in re.findall(r"^##[ \t]+(.+?)[ \t]*$", candidate, re.MULTILINE)}
    missing_headings = [heading for heading in headings if heading not in present_headings]
    sensitive_lines = _unredacted_secret_lines(candidate)
    unsafe = bool(sensitive_lines)
    unsupported_claims = _unsupported_claims(
        candidate, facts, claim_threshold_percent, ngram_overlap_weight_percent,
        semantic_weight_percent, uncertainty_margin_percent,
    ) if source.get("kind") == "daily" else []
    for claim in unsupported_claims:
        matched = claim["matched_fact_index"]
        claim["matched_evidence_id"] = fact_ids[matched] if matched is not None else None
    coverage = 1.0 - len(missing_facts) / len(facts) if facts else 1.0
    metrics = {
        "fact_coverage": round(coverage, 4),
        "heading_score": round((len(headings) - len(missing_headings)) / len(headings), 4),
        "safety": 0.0 if unsafe else 1.0,
    }
    return {
        "passed": coverage >= 0.8 and not missing_headings and not unsafe and not unsupported_claims,
        "missing_fact_indexes": missing_facts,
        "missing_evidence_ids": [fact_ids[index] for index in missing_facts],
        "fact_match_scores": fact_match_scores,
        "missing_headings": missing_headings,
        "unsupported_claims": unsupported_claims,
        "unsafe": unsafe,
        "sensitive_lines": sensitive_lines,
        "metrics": metrics,
    }


def summarize_edit_feedback(initial_draft: str, final_text: str) -> Dict[str, Any]:
    """Keep useful edit signals without copying report content into feedback records."""
    return {
        "changed": initial_draft != final_text,
        "similarity": round(SequenceMatcher(None, _normalize(initial_draft), _normalize(final_text)).ratio(), 4),
        "character_delta": len(final_text) - len(initial_draft),
        "list_style_before": _list_style(initial_draft),
        "list_style_after": _list_style(final_text),
        "statistics_before": "## 开发统计" in initial_draft,
        "statistics_after": "## 开发统计" in final_text,
        "max_item_length_before": _max_report_item_length(initial_draft),
        "max_item_length_after": _max_report_item_length(final_text),
        "headings_added": [heading for heading in REQUIRED_HEADINGS if heading not in initial_draft and heading in final_text],
        "headings_removed": [heading for heading in REQUIRED_HEADINGS if heading in initial_draft and heading not in final_text],
    }


def _max_report_item_length(text: str) -> int:
    lengths = []
    for line in text.splitlines():
        if not re.match(r"^\s*(?:[-*]|\d+[.)、])\s+", line):
            continue
        value = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s+", "", line).strip()
        if value and value != "暂无已记录内容":
            lengths.append(len(value))
    return max(lengths, default=0)


def score_report(source: Dict[str, Any], candidate: str, expected: str) -> Dict[str, float]:
    facts = [str(item).strip() for item in source.get("facts", []) if str(item).strip()]
    fact_coverage = mean([_contains_fact(candidate, fact) for fact in facts]) if facts else 1.0
    heading_score = mean([1.0 if heading in candidate else 0.0 for heading in REQUIRED_HEADINGS])
    safety = 0.0 if _has_unredacted_secret(candidate) else 1.0
    similarity = SequenceMatcher(None, _normalize(candidate), _normalize(expected)).ratio()
    total = 0.35 * fact_coverage + 0.2 * heading_score + 0.25 * safety + 0.2 * similarity
    return {
        "score": round(total, 4),
        "fact_coverage": round(fact_coverage, 4),
        "heading_score": round(heading_score, 4),
        "safety": round(safety, 4),
        "similarity": round(similarity, 4),
    }


def aggregate(results: Iterable[Dict[str, float]]) -> Dict[str, float]:
    values = list(results)
    if not values:
        return {"score": 0.0, "fact_coverage": 0.0, "heading_score": 0.0, "safety": 0.0, "similarity": 0.0}
    return {key: round(mean(item[key] for item in values), 4) for key in values[0]}


def _contains_fact(candidate: str, fact: str) -> float:
    normalized_candidate = _normalize(candidate)
    normalized_fact = _normalize(fact)
    if not normalized_fact:
        return 1.0
    if normalized_fact in normalized_candidate:
        return 1.0
    words = [item for item in re.split(r"[\s,，。:：;；/]+", normalized_fact) if len(item) >= 2]
    if not words:
        return 0.0
    return sum(1 for word in words if word in normalized_candidate) / len(words)


def _unsupported_claims(candidate: str, facts: List[str], threshold_percent: int,
                        ngram_overlap_weight_percent: int, semantic_weight_percent: int,
                        uncertainty_margin_percent: int) -> List[Dict[str, Any]]:
    issues = []
    semantic_mode = semantic_weight_percent > 0
    for item in review_claims(candidate, semantic_mode=semantic_mode):
        normalized_claim = _normalize(item["text"])
        details = []
        for fact in facts:
            variants = _fact_match_variants(fact, semantic_mode)
            variant_details = [_claim_match_details(
                normalized_claim, variant, ngram_overlap_weight_percent,
                semantic_weight_percent,
            ) for variant in variants]
            details.append(max(variant_details, key=lambda value: value["score"]))
        scores = [detail["score"] for detail in details]
        best_score = max(scores, default=0.0)
        if best_score < threshold_percent / 100:
            best_index = scores.index(best_score) if scores else None
            status = ("uncertain" if best_score >=
                      (threshold_percent - uncertainty_margin_percent) / 100 else "unsupported")
            issues.append({
                "line": item["line"], "column_start": item["column_start"],
                "column_end": item["column_end"],
                "matched_fact_index": best_index,
                "match_score": round(best_score, 4),
                "support_status": status,
                "structure": {key: item["structure"][key]
                              for key in ("action", "action_family", "polarity")},
                "score_components": details[best_index]["components"] if best_index is not None else {
                    "sequence": 0.0, "ngram": 0.0, "semantic": 0.0,
                },
            })
    return issues


def review_claims(candidate: str, semantic_mode: bool = False) -> List[Dict[str, Any]]:
    claims = []
    section = ""
    for line_number, line in enumerate(candidate.splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section != "今日完成":
            continue
        match = re.match(r"^\s*(?:[-*]|\d+[.)、])\s+(.+)$", line)
        if not match:
            continue
        for claim, start, end in _claim_segments(match.group(1), semantic_mode=semantic_mode):
            normalized_claim = _normalize(redact_sensitive(claim))
            if (
                len(normalized_claim) < 6
                and not (len(normalized_claim) >= 3 and ACTION_START.match(normalized_claim))
            ) or claim == "暂无已记录内容":
                continue
            claims.append({"index": len(claims), "line": line_number,
                           "column_start": match.start(1) + start,
                           "column_end": match.start(1) + end,
                           "text": redact_sensitive(claim),
                           "structure": structured_claim(claim)})
    return claims


def review_completion_lines(candidate: str) -> List[Dict[str, Any]]:
    lines = []
    in_section = False
    for line_number, line in enumerate(candidate.splitlines(), 1):
        if line.startswith("## "):
            in_section = line[3:].strip() == "今日完成"
        elif in_section and line.strip():
            lines.append({"line": line_number, "text": line})
    return lines


def _claim_match_score(claim: str, fact: str, ngram_overlap_weight_percent: int = 0) -> float:
    if not fact:
        return 0.0
    if claim == fact or ("：" in claim and claim.split("：", 1)[1] == fact):
        return 1.0
    sequence_score = SequenceMatcher(None, claim, fact).ratio()
    ngram_score = _ngram_overlap(claim, fact)
    score = ((100 - ngram_overlap_weight_percent) * sequence_score +
             ngram_overlap_weight_percent * ngram_score) / 100
    claim_body = re.sub(r"^(?:已经|已)?完成|(?:已经|已)?完成$", "", claim)
    fact_body = re.sub(r"^(?:已经|已)?完成|(?:已经|已)?完成$", "", fact)
    if len(claim_body) >= 4 and len(fact_body) >= 4:
        score = min(score, SequenceMatcher(None, claim_body, fact_body).ratio())
    claim_action = ACTION_START.match(claim)
    fact_action = ACTION_START.match(fact)
    if claim_action and fact_action and claim_action.group().removeprefix("已") != fact_action.group().removeprefix("已"):
        score = min(score, 0.49)
    if bool(re.match(r"^(?:未|尚未|待|计划)", claim)) != bool(re.match(r"^(?:未|尚未|待|计划)", fact)):
        score = min(score, 0.49)
    return score


def _claim_match_details(claim: str, fact: str, ngram_overlap_weight_percent: int,
                         semantic_weight_percent: int) -> Dict[str, Any]:
    sequence = SequenceMatcher(None, claim, fact).ratio() if fact else 0.0
    ngram = _ngram_overlap(claim, fact) if fact else 0.0
    legacy = _claim_match_score(claim, fact, ngram_overlap_weight_percent)
    semantic = _structured_similarity(structured_claim(claim), structured_claim(fact))
    score = ((100 - semantic_weight_percent) * legacy + semantic_weight_percent * semantic) / 100
    return {"score": score, "components": {
        "sequence": round(sequence, 4), "ngram": round(ngram, 4),
        "semantic": round(semantic, 4),
    }}


def structured_claim(value: str) -> Dict[str, Any]:
    normalized = _normalize(redact_sensitive(value))
    context = ""
    body = normalized
    if "：" in body:
        prefix, suffix = body.split("：", 1)
        if suffix:
            context, body = prefix, suffix
    polarity = "pending" if re.match(r"^(?:未|尚未|待|计划)", body) else "completed"
    match = ACTION_ANYWHERE.search(body)
    action = match.group("action") if match else ""
    action_family = ACTION_FAMILIES.get(action, "")
    object_text = body[match.end():] if match else body
    object_text = re.sub(r"^(?:了|本次|相关|对)", "", object_text)
    semantic_object = object_text
    for pattern, replacement in SEMANTIC_ALIASES:
        semantic_object = pattern.sub(replacement, semantic_object)
    return {"context": context, "action": action, "action_family": action_family,
            "polarity": polarity, "object": object_text,
            "semantic_object": semantic_object}


def _structured_similarity(left: Dict[str, Any], right: Dict[str, Any]) -> float:
    if left["polarity"] != right["polarity"]:
        return 0.0
    if left["action"] and right["action"]:
        action_score = (1.0 if left["action"] == right["action"] else
                        0.85 if left["action_family"] == right["action_family"] else 0.0)
    else:
        action_score = 0.5
    left_object, right_object = left["semantic_object"], right["semantic_object"]
    if left_object and right_object and (left_object in right_object or right_object in left_object):
        object_score = 1.0
    else:
        object_score = max(
            SequenceMatcher(None, left_object, right_object).ratio(),
            _ngram_overlap(left_object, right_object),
        ) if left_object and right_object else 0.0
    context_score = (1.0 if left["context"] and right["context"] and
                     (left["context"] in right["context"] or right["context"] in left["context"])
                     else 0.5)
    return 0.25 * action_score + 0.65 * object_score + 0.1 * context_score


def _ngram_overlap(left: str, right: str) -> float:
    def grams(value: str) -> set[str]:
        return {value[index:index + 2] for index in range(max(0, len(value) - 1))}

    left_grams = grams(left)
    right_grams = grams(right)
    if not left_grams or not right_grams:
        return 1.0 if left == right else 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _claim_segments(claim: str, semantic_mode: bool = False) -> List[tuple[str, int, int]]:
    boundaries = []
    start = 0
    for separator in CLAIM_SEPARATORS.finditer(claim):
        boundaries.append((start, separator.start()))
        start = separator.end()
    boundaries.append((start, len(claim)))
    segments = []
    for start, end in boundaries:
        raw = claim[start:end]
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        if right > left:
            segments.append((raw[left:right], start + left, start + right))
    if not semantic_mode or len(segments) < 2:
        return segments
    merged = []
    for segment in segments:
        if merged and not ACTION_START.match(_normalize(segment[0])):
            previous, previous_start, _ = merged[-1]
            merged[-1] = (claim[previous_start:segment[2]], previous_start, segment[2])
        else:
            merged.append(segment)
    return merged


def _fact_match_variants(fact: str, semantic_mode: bool) -> List[str]:
    normalized = _normalize(fact)
    if not semantic_mode:
        return [normalized]
    variants = [normalized]
    for segment, _, _ in _claim_segments(fact):
        value = _normalize(segment)
        if value and value not in variants:
            variants.append(value)
    return variants


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _list_style(value: str) -> str:
    numbered = sum(bool(re.match(r"^\s*\d+\.\s+", line)) for line in value.splitlines())
    bullets = sum(bool(re.match(r"^\s*[-*]\s+", line)) for line in value.splitlines())
    if not numbered and not bullets:
        return "none"
    return "numbered" if numbered >= bullets else "bulleted"


def _has_unredacted_secret(value: str) -> bool:
    return bool(_unredacted_secret_lines(value))


def _unredacted_secret_lines(value: str) -> List[int]:
    return sorted({
        value.count("\n", 0, match.start()) + 1
        for pattern in SECRET_PATTERNS
        for match in pattern.finditer(value)
        if not re.search(r"[:=]\s*\[REDACTED\]$", match.group(0), re.IGNORECASE)
    })
