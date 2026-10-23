from __future__ import annotations

import hashlib

from .report_generator import redact_sensitive


WRITER_BASE_INSTRUCTION = (
    "你是中文实习日报 Writer。仅润色以下已脱敏日报，不得新增、删除或猜测工作事实；"
    "必须保留‘今日完成’、‘遇到的问题’、‘明日计划’三个标题。只输出 Markdown 日报正文。"
)
CRITIC_RULE_VERSION = 1


def writer_instruction(addendum: str) -> str:
    return WRITER_BASE_INSTRUCTION + (f"\n写作风格补充：{addendum}" if addendum else "")


def validate_writer_addendum(value: str) -> str:
    addendum = value.strip()
    if not addendum or len(addendum) > 500 or any(ord(char) < 32 and char not in "\n\t" for char in addendum):
        raise ValueError("写作风格补充需为 1 到 500 字的普通文本")
    if redact_sensitive(addendum) != addendum:
        raise ValueError("写作风格补充不能包含敏感信息")
    return addendum


def instruction_hash(instruction: str) -> str:
    return hashlib.sha256(instruction.encode("utf-8")).hexdigest()
