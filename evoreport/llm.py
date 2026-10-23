from __future__ import annotations

import json
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Tuple, TypeVar
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import Settings
from .agent_instructions import writer_instruction

T = TypeVar("T")
MODEL_CALL_SECONDS = 20.0
_model_call_slots = threading.BoundedSemaphore(4)


class ModelCallTimeout(TimeoutError):
    pass


def call_with_deadline(call: Callable[[], T], seconds: float) -> T:
    """Bound caller wait; abandoned workers never receive access to runtime state."""
    deadline = time.monotonic() + max(0.0, seconds)
    slots = _model_call_slots
    if seconds <= 0 or not slots.acquire(timeout=seconds):
        raise ModelCallTimeout("模型调用等待超时")
    results: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            results.put((True, call()))
        except BaseException as exc:
            results.put((False, exc))
        finally:
            slots.release()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except BaseException:
        slots.release()
        raise
    try:
        success, result = results.get(timeout=max(0.0, deadline - time.monotonic()))
    except queue.Empty:
        raise ModelCallTimeout("模型调用超时") from None
    if time.monotonic() >= deadline:
        raise ModelCallTimeout("模型调用超时")
    if not success:
        raise result
    return result


@dataclass(frozen=True)
class ArkReportWriter:
    base_url: str
    model: str
    api_key: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "ArkReportWriter":
        return cls(settings.ark_base_url, settings.ark_model, settings.ark_api_key)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)

    def rewrite(self, draft: str, instruction: str | None = None) -> Tuple[str, bool, str]:
        text, used, note, _ = self.rewrite_with_usage(draft, instruction)
        return text, used, note

    @staticmethod
    def estimate_input_tokens(draft: str, instruction: str | None = None) -> int:
        prompt = (instruction if instruction is not None else writer_instruction("")) + "\n\n" + draft
        return max(1, math.ceil(len(prompt.encode("utf-8")) / 4))

    def rewrite_with_usage(
        self, draft: str, instruction: str | None = None, max_output_tokens: int | None = None,
    ) -> tuple[str, bool, str, dict | None]:
        if not self.enabled:
            return draft, False, "未配置 ARK_API_KEY，使用本地确定性 Writer", None
        prompt = (instruction if instruction is not None else writer_instruction("")) + "\n\n" + draft
        request_data = {
            "model": self.model,
            "stream": False,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        }
        if max_output_tokens is not None:
            request_data["max_output_tokens"] = max_output_tokens
        body = json.dumps(request_data, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}/responses", data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = _response_text(payload).strip()
            usage = _response_usage(payload, prompt, text)
            if not text or len(text) > 20000:
                return draft, False, "模型返回为空或超出长度限制，使用本地 Writer", usage
            return text + "\n", True, f"方舟模型 {self.model} 已完成润色", usage
        except TimeoutError as exc:
            raise ModelCallTimeout("模型请求超时") from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ModelCallTimeout("模型请求超时") from exc
            return draft, False, "模型调用失败，已自动回退到本地 Writer", None
        except (ValueError, json.JSONDecodeError):
            return draft, False, "模型调用失败，已自动回退到本地 Writer", None


def _response_usage(payload: dict, prompt: str, output: str) -> dict:
    usage = payload.get("usage")
    if isinstance(usage, dict):
        input_count = usage.get("input_tokens")
        output_count = usage.get("output_tokens")
        total = usage.get("total_tokens")
        if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in (input_count, output_count, total)) and total >= input_count + output_count:
            return {"input_tokens": input_count, "output_tokens": output_count,
                    "total_tokens": total, "source": "provider"}
    input_count = max(1, math.ceil(len(prompt.encode("utf-8")) / 4))
    output_count = math.ceil(len(output.encode("utf-8")) / 4)
    return {"input_tokens": input_count, "output_tokens": output_count,
            "total_tokens": input_count + output_count, "source": "estimated"}


def _response_text(payload: dict) -> str:
    parts = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)
