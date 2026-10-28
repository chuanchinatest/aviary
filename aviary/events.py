"""内部统一事件模型:所有上游适配器都把回复流规范化为这里的事件序列。

事件流约定(一次回复):
  ("text", delta_str)                    —— 正文增量
  ("tool", index, id, name, args_delta)  —— 工具调用:首次出现给 id/name,之后 args 为增量
  ("usage", prompt_tokens, completion_tokens)   —— 可选,0 表示未知
  ("end", finish_reason)                 —— finish_reason ∈ {stop, tool_calls, length}
"""

from __future__ import annotations

from dataclasses import dataclass, field

FINISH_MAP_TO_OPENAI = {
    "end_turn": "stop",
    "stop": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "tool_calls": "tool_calls",
    "max_tokens": "length",
    "length": "length",
    "function_call": "tool_calls",
}

FINISH_MAP_TO_ANTHROPIC = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


@dataclass
class URequest:
    """协议无关的请求(已解析到"上游模型名"层级)。"""

    model: str
    messages: list[dict]  # [{"role": "user"|"assistant"|"tool", "content": str, "tool_calls": [...], "tool_call_id": ...}]
    system: str | None = None
    tools: list[dict] | None = None  # [{"name","description","parameters"}]
    max_tokens: int = 4096
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    stop: list[str] | None = None
    raw: dict | None = None  # 原始请求体,同协议透传时用


def text_delta(s: str) -> tuple:
    return ("text", s)


def tool_delta(index: int, id_: str | None, name: str | None, args: str) -> tuple:
    return ("tool", index, id_, name, args)


def usage_event(p: int, c: int) -> tuple:
    return ("usage", int(p or 0), int(c or 0))


def end_event(reason: str | None) -> tuple:
    return ("end", FINISH_MAP_TO_OPENAI.get(reason or "stop", "stop"))
