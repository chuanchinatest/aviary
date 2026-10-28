"""流式翻译:上游 SSE → 统一事件 → 客户端协议的 SSE。

两条链路:
  Anthropic 上游 ──► anthropic_sse_to_universe ──► universe_to_openai_sse ──► OpenAI 客户端
  OpenAI 上游   ──► openai_sse_to_universe    ──► universe_to_anthropic_sse ──► Anthropic 客户端
同协议时也走统一层,以便集中做用量统计与日志。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator


def _sse_data(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


async def openai_sse_to_universe(lines: AsyncIterator[str]) -> AsyncIterator[tuple]:
    args_state: dict[int, str] = {}
    async for line in lines:
        data = _sse_data(line)
        if data is None:
            continue
        for ch in data.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                yield ("text", delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index") or 0)
                fn = tc.get("function") or {}
                piece = fn.get("arguments") or ""
                args_state[idx] = args_state.get(idx, "") + piece
                yield ("tool", idx, tc.get("id"), fn.get("name"), piece)
            if ch.get("finish_reason"):
                yield ("end", ch["finish_reason"])
        if data.get("usage"):
            u = data["usage"]
            yield ("usage", u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


async def anthropic_sse_to_universe(lines: AsyncIterator[str]) -> AsyncIterator[tuple]:
    tool_of_block: dict[int, int] = {}
    input_tokens = 0
    output_tokens = 0
    stop_reason = None
    async for line in lines:
        data = _sse_data(line)
        if data is None:
            continue
        etype = data.get("type")
        if etype == "message_start":
            input_tokens = (data.get("message", {}).get("usage") or {}).get("input_tokens", 0)
        elif etype == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = int(data.get("index") or 0)
                tool_of_block[idx] = len(tool_of_block)
                yield ("tool", tool_of_block[idx], block.get("id"), block.get("name"), "")
        elif etype == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield ("text", delta["text"])
            elif delta.get("type") == "input_json_delta":
                idx = tool_of_block.get(int(data.get("index") or 0), 0)
                yield ("tool", idx, None, None, delta.get("partial_json") or "")
        elif etype == "message_delta":
            d = data.get("delta") or {}
            if d.get("stop_reason"):
                stop_reason = d["stop_reason"]
            u = data.get("usage") or {}
            output_tokens = u.get("output_tokens", output_tokens)
        elif etype == "message_stop":
            yield ("usage", input_tokens, output_tokens)
            yield ("end", stop_reason or "end_turn")


async def universe_to_openai_sse(
    events: AsyncIterator[tuple], model: str
) -> AsyncIterator[str]:
    def chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
        obj: dict = {
            "id": "chatcmpl-aviary", "object": "chat.completion.chunk",
            "created": 0, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage is not None:
            obj["usage"] = usage
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    usage_acc = {"prompt_tokens": 0, "completion_tokens": 0}
    yield chunk({"role": "assistant"})
    async for ev in events:
        kind = ev[0]
        if kind == "text":
            yield chunk({"content": ev[1]})
        elif kind == "tool":
            _, index, id_, name, args_piece = ev
            delta: dict = {"tool_calls": [{"index": index, "function": {"arguments": args_piece}}]}
            if id_:
                delta["tool_calls"][0]["id"] = id_
                delta["tool_calls"][0]["type"] = "function"
            if name:
                delta["tool_calls"][0]["function"]["name"] = name
            yield chunk(delta)
        elif kind == "usage":
            usage_acc = {"prompt_tokens": ev[1], "completion_tokens": ev[2]}
    yield chunk({}, finish="stop")
    yield chunk({}, finish=None, usage={
        **usage_acc, "total_tokens": usage_acc["prompt_tokens"] + usage_acc["completion_tokens"]})
    yield "data: [DONE]\n\n"


async def universe_to_anthropic_sse(
    events: AsyncIterator[tuple], model: str
) -> AsyncIterator[str]:
    def event(name: str, payload: dict) -> str:
        payload = {"type": name, **payload}
        return f"event: {name}\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    block_index = 0          # 当前内容块的 anthropic index
    tool_index = -1          # 第几个 tool_use 块
    text_open = False
    open_tool_blocks: dict[int, int] = {}  # 统一 tool index → anthropic block index
    usage = {"input_tokens": 0, "output_tokens": 0}
    finish = "end_turn"

    yield event("message_start", {"message": {
        "id": "msg_aviary", "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0}}})
    yield event("ping", {})

    async for ev in events:
        kind = ev[0]
        if kind == "text":
            if not text_open:
                yield event("content_block_start",
                            {"index": block_index, "content_block": {"type": "text", "text": ""}})
                text_open = True
            yield event("content_block_delta",
                        {"index": block_index, "delta": {"type": "text_delta", "text": ev[1]}})
        elif kind == "tool":
            _, index, id_, name, args_piece = ev
            if index not in open_tool_blocks:
                if text_open:
                    yield event("content_block_stop", {"index": block_index})
                    text_open = False
                block_index += 1
                open_tool_blocks[index] = block_index
                yield event("content_block_start", {
                    "index": block_index,
                    "content_block": {"type": "tool_use", "id": id_ or "toolu_0",
                                      "name": name or "tool", "input": {}}})
            if args_piece:
                yield event("content_block_delta", {
                    "index": open_tool_blocks[index],
                    "delta": {"type": "input_json_delta", "partial_json": args_piece}})
        elif kind == "usage":
            usage = {"input_tokens": ev[1], "output_tokens": ev[2]}
        elif kind == "end":
            finish = {"stop": "end_turn", "tool_calls": "tool_use",
                      "length": "max_tokens"}.get(ev[1], "end_turn")

    if text_open or not open_tool_blocks:
        if not text_open:
            yield event("content_block_start",
                        {"index": block_index, "content_block": {"type": "text", "text": ""}})
            text_open = True
        yield event("content_block_stop", {"index": block_index})
    for idx in open_tool_blocks.values():
        yield event("content_block_stop", {"index": idx})
    yield event("message_delta", {"delta": {"stop_reason": finish, "stop_sequence": None},
                                  "usage": {"output_tokens": usage["output_tokens"]}})
    yield event("message_stop", {})
