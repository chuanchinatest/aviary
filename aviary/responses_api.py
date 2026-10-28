"""OpenAI Responses API 客户端协议(/v1/responses):解析 → 统一请求;统一元数据 → 响应/流式事件。

上游一律走 Chat Completions 适配器(所有 OpenAI 兼容与 Anthropic 型上游都支持)。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from .events import URequest


def responses_request_to_unified(body: dict, model: str) -> URequest:
    msgs: list[dict] = []
    inp = body.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
    else:
        for item in inp or []:
            role = item.get("role", "user")
            content = item.get("content")
            if isinstance(content, str):
                msgs.append({"role": role, "content": content})
                continue
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for c in content or []:
                ctype = c.get("type", "")
                if ctype in ("input_text", "output_text", "text"):
                    text_parts.append(c.get("text", ""))
                elif ctype in ("function_call", "tool_call"):
                    tool_calls.append({"id": c.get("call_id") or c.get("id") or "call",
                                       "type": "function",
                                       "function": {"name": c["name"],
                                                    "arguments": c.get("arguments") or "{}"}})
                elif ctype in ("function_call_output", "tool_result"):
                    out = c.get("output")
                    msgs.append({"role": "tool",
                                 "tool_call_id": c.get("call_id") or "call",
                                 "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)})
            if tool_calls:
                m: dict = {"role": "assistant", "content": "".join(text_parts) or None,
                           "tool_calls": tool_calls}
                msgs.append(m)
            elif text_parts:
                msgs.append({"role": role, "content": "".join(text_parts)})
    tools = [
        {"name": t["name"], "description": t.get("description", ""),
         "parameters": t.get("parameters") or {"type": "object"}}
        for t in (body.get("tools") or []) if t.get("type") == "function"
    ] or None
    return URequest(
        model=model, messages=msgs, system=body.get("instructions"),
        tools=tools,
        max_tokens=body.get("max_output_tokens") or 4096,
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        stream=bool(body.get("stream")),
        raw=body,
    )


def unified_to_responses_response(meta: dict, model: str, rid: str = "resp_aviary") -> dict:
    output: list[dict] = []
    if meta.get("text"):
        output.append({"type": "message", "id": "msg_0", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": meta["text"],
                                    "annotations": []}]})
    for i, tc in enumerate(meta.get("tool_calls") or []):
        output.append({"type": "function_call", "id": f"fc_{i}",
                       "call_id": tc.get("id") or f"call_{i}", "name": tc["function"]["name"],
                       "arguments": tc["function"].get("arguments") or "{}",
                       "status": "completed"})
    usage = meta.get("usage") or {}
    pin, pout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    return {
        "id": rid, "object": "response", "created_at": 0, "status": "completed",
        "model": model, "output": output, "parallel_tool_calls": True,
        "usage": {"input_tokens": pin, "output_tokens": pout, "total_tokens": pin + pout},
    }


async def universe_to_responses_sse(
    events: AsyncIterator[tuple], model: str, rid: str = "resp_aviary"
) -> AsyncIterator[str]:
    def ev(name: str, payload: dict) -> str:
        return f"event: {name}\ndata: " + json.dumps({"type": name, **payload},
                                                     ensure_ascii=False) + "\n\n"

    def base_response(output: list[dict] | None = None, status: str = "in_progress") -> dict:
        return {"id": rid, "object": "response", "created_at": 0, "status": status,
                "model": model, "output": output or [], "usage": None}

    seq = 0
    text = ""
    tool_calls: list[dict] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    yield ev("response.created", {"response": base_response(), "sequence_number": seq})
    seq += 1
    open_item = False
    async for e in events:
        kind = e[0]
        if kind == "text":
            if not open_item:
                yield ev("response.output_item.added", {
                    "output_index": 0, "sequence_number": seq,
                    "item": {"type": "message", "id": "msg_0", "role": "assistant",
                             "status": "in_progress", "content": []}})
                seq += 1
                open_item = True
            text += e[1]
            yield ev("response.output_text.delta", {
                "item_id": "msg_0", "output_index": 0, "content_index": 0,
                "sequence_number": seq, "delta": e[1]})
            seq += 1
        elif kind == "tool":
            _, index, id_, name, args_piece = ev
            if index >= len(tool_calls):
                tool_calls.append({"call_id": id_ or f"call_{index}",
                                   "name": name or "tool", "arguments": ""})
            tool_calls[index]["arguments"] += args_piece or ""
        elif kind == "usage":
            usage = {"input_tokens": e[1], "output_tokens": e[2]}
    if open_item:
        yield ev("response.output_text.done", {"item_id": "msg_0", "output_index": 0,
                 "content_index": 0, "sequence_number": seq, "text": text})
        seq += 1
        yield ev("response.output_item.done", {
            "output_index": 0, "sequence_number": seq,
            "item": {"type": "message", "id": "msg_0", "role": "assistant",
                     "status": "completed",
                     "content": [{"type": "output_text", "text": text, "annotations": []}]}})
        seq += 1
    final_output: list[dict] = []
    if text or not tool_calls:
        final_output.append({"type": "message", "id": "msg_0", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": text, "annotations": []}]})
    for i, tc in enumerate(tool_calls):
        final_output.append({"type": "function_call", "id": f"fc_{i}",
                             "call_id": tc["call_id"], "name": tc["name"],
                             "arguments": tc["arguments"], "status": "completed"})
    final = base_response(final_output, "completed")
    final["usage"] = {"input_tokens": usage["input_tokens"],
                      "output_tokens": usage["output_tokens"],
                      "total_tokens": usage["input_tokens"] + usage["output_tokens"]}
    yield ev("response.completed", {"response": final, "sequence_number": seq})
