"""协议翻译:OpenAI Chat ↔ Anthropic Messages(请求、响应、流式事件)。"""

from __future__ import annotations

import json

from .events import FINISH_MAP_TO_ANTHROPIC, FINISH_MAP_TO_OPENAI, URequest


# ---------------------------------------------------------------------------
# 请求翻译
# ---------------------------------------------------------------------------
def openai_request_to_unified(body: dict, model: str) -> URequest:
    msgs: list[dict] = []
    system = None
    for m in body.get("messages", []):
        role = m.get("role")
        if role == "system":
            system = m.get("content") or None
            continue
        msgs.append(dict(m))
    tools = [
        {"name": t["function"]["name"],
         "description": t["function"].get("description", ""),
         "parameters": t["function"].get("parameters") or {"type": "object"}}
        for t in (body.get("tools") or []) if "function" in t
    ] or None
    return URequest(
        model=model,
        messages=msgs,
        system=system,
        tools=tools,
        max_tokens=body.get("max_tokens") or 4096,
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        stream=bool(body.get("stream")),
        stop=body.get("stop"),
        raw=body,
    )


def unified_to_openai_request(u: URequest) -> dict:
    body: dict = {
        "model": u.model,
        "messages": ([{"role": "system", "content": u.system}] if u.system else []) + u.messages,
        "max_tokens": u.max_tokens,
        "stream": u.stream,
    }
    if u.temperature is not None:
        body["temperature"] = u.temperature
    if u.top_p is not None:
        body["top_p"] = u.top_p
    if u.stop:
        body["stop"] = u.stop
    if u.tools:
        body["tools"] = [
            {"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("parameters") or {"type": "object"}}}
            for t in u.tools
        ]
        if u.raw and u.raw.get("tool_choice"):
            body["tool_choice"] = u.raw["tool_choice"]
    return body


def anthropic_request_to_unified(body: dict, model: str) -> URequest:
    msgs: list[dict] = []
    for m in body.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []
        for block in content or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block["id"], "type": "function",
                    "function": {"name": block["name"],
                                 "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)},
                })
            elif btype == "tool_result":
                inner = block.get("content")
                text = inner if isinstance(inner, str) else "".join(
                    b.get("text", "") for b in (inner or []) if isinstance(b, dict))
                tool_results.append({"tool_call_id": block["tool_use_id"], "content": text, "role": "tool"})
        if tool_results:  # tool_result 以独立 tool 消息表达
            msgs.extend(tool_results)
        else:
            msg: dict = {"role": role, "content": "".join(text_parts)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
                msg["content"] = "".join(text_parts) or None
            msgs.append(msg)
    tools = [
        {"name": t["name"], "description": t.get("description", ""),
         "parameters": t.get("input_schema") or {"type": "object"}}
        for t in (body.get("tools") or [])
    ] or None
    sys = body.get("system")
    if isinstance(sys, list):
        sys = "".join(b.get("text", "") for b in sys if isinstance(b, dict))
    return URequest(
        model=model,
        messages=msgs,
        system=sys or None,
        tools=tools,
        max_tokens=body.get("max_tokens") or 4096,
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        stream=bool(body.get("stream")),
        stop=None,
        raw=body,
    )


# ---------------------------------------------------------------------------
# 响应与流式翻译
# ---------------------------------------------------------------------------
def openai_response_to_unified(data: dict) -> tuple[str, dict]:
    """OpenAI 非流式响应 → (完整文本, 元数据)。"""
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    meta = {
        "text": msg.get("content") or "",
        "tool_calls": msg.get("tool_calls") or [],
        "finish_reason": FINISH_MAP_TO_OPENAI.get(choice.get("finish_reason") or "stop", "stop"),
        "usage": data.get("usage") or {},
    }
    return meta["text"], meta


def unified_to_anthropic_response(meta: dict, model: str) -> dict:
    """统一元数据 → Anthropic 非流式响应。"""
    content: list[dict] = []
    if meta.get("text"):
        content.append({"type": "text", "text": meta["text"]})
    for tc in meta.get("tool_calls") or []:
        args = tc["function"].get("arguments") or "{}"
        content.append({
            "type": "tool_use", "id": tc.get("id") or "toolu_0",
            "name": tc["function"]["name"],
            "input": json.loads(args) if isinstance(args, str) and args.strip() else (args or {}),
        })
    usage = meta.get("usage") or {}
    stop = FINISH_MAP_TO_ANTHROPIC.get(meta.get("finish_reason") or "stop", "end_turn")
    if stop == "tool_use" and not any(c["type"] == "tool_use" for c in content):
        stop = "end_turn"
    return {
        "id": "msg_aviary", "type": "message", "role": "assistant", "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


def anthropic_response_to_unified(data: dict) -> tuple[str, dict]:
    """Anthropic 非流式响应 → (完整文本, 统一元数据, OpenAI 形态 tool_calls)。"""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block["id"], "type": "function",
                "function": {"name": block["name"],
                             "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)},
            })
    usage = data.get("usage") or {}
    meta = {
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
        "finish_reason": FINISH_MAP_TO_OPENAI.get(data.get("stop_reason") or "end_turn", "stop"),
        "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                  "completion_tokens": usage.get("output_tokens", 0)},
    }
    return meta["text"], meta


def unified_to_openai_response(meta: dict, model: str) -> dict:
    message: dict = {"role": "assistant", "content": meta.get("text") or None}
    if meta.get("tool_calls"):
        message["tool_calls"] = meta["tool_calls"]
    usage = meta.get("usage") or {}
    return {
        "id": "chatcmpl-aviary", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": meta.get("finish_reason") or "stop"}],
        "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                  "completion_tokens": usage.get("completion_tokens", 0),
                  "total_tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)},
    }



def unified_to_anthropic_request(u: URequest) -> dict:
    msgs: list[dict] = []
    for m in u.messages:
        role = m.get("role")
        if role == "tool":  # 工具结果 → user 的 tool_result 块
            msgs.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                             "content": m.get("content") or ""}],
            })
            continue
        content = m.get("content")
        tool_calls = m.get("tool_calls")
        if role == "assistant" and tool_calls:
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in tool_calls:
                args = tc["function"].get("arguments") or "{}"
                blocks.append({"type": "tool_use", "id": tc["id"],
                               "name": tc["function"]["name"],
                               "input": json.loads(args) if isinstance(args, str) else args})
            msgs.append({"role": "assistant", "content": blocks})
        else:
            msgs.append({"role": role, "content": content or ""})
    body: dict = {
        "model": u.model,
        "messages": msgs,
        "max_tokens": u.max_tokens,
        "stream": u.stream,
    }
    if u.system:
        body["system"] = u.system
    if u.temperature is not None:
        body["temperature"] = u.temperature
    if u.top_p is not None:
        body["top_p"] = u.top_p
    if u.tools:
        body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                          "input_schema": t.get("parameters") or {"type": "object"}} for t in u.tools]
    tc = (u.raw or {}).get("tool_choice") if u.raw else None
    if u.tools and tc:
        if tc == "auto":
            body["tool_choice"] = {"type": "auto"}
        elif tc == "none":
            body["tool_choice"] = {"type": "none"}
        elif isinstance(tc, dict) and tc.get("type") == "function":
            body["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}
    return body
