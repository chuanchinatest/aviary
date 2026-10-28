"""Gemini 流式:上游 SSE → 统一事件;统一事件 → 客户端 alt=sse 流。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator


def _data(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


async def gemini_sse_to_universe(lines: AsyncIterator[str]) -> AsyncIterator[tuple]:
    async for line in lines:
        data = _data(line)
        if data is None:
            continue
        for cand in data.get("candidates") or []:
            parts = (cand.get("content") or {}).get("parts") or []
            for p in parts:
                if "text" in p and p["text"]:
                    yield ("text", p["text"])
                elif "functionCall" in p:
                    fc = p["functionCall"]
                    yield ("tool", 0, fc.get("name", "call"), fc["name"],
                           json.dumps(fc.get("args", {}), ensure_ascii=False))
            if cand.get("finishReason"):
                yield ("end", {"STOP": "stop", "MAX_TOKENS": "length"}.get(
                    cand["finishReason"], "stop"))
        u = data.get("usageMetadata") or {}
        if u:
            yield ("usage", u.get("promptTokenCount", 0),
                   u.get("candidatesTokenCount", 0))


async def universe_to_gemini_sse(
    events: AsyncIterator[tuple], model: str
) -> AsyncIterator[str]:
    def chunk(payload: dict) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    usage = {"promptTokenCount": 0, "candidatesTokenCount": 0}
    finish = "STOP"
    async for ev in events:
        kind = ev[0]
        if kind == "text":
            yield chunk({"candidates": [{"content": {"role": "model",
                       "parts": [{"text": ev[1]}]}, "index": 0}],
                       "modelVersion": model})
        elif kind == "tool":
            _, index, id_, name, args_piece = ev
            try:
                args = json.loads(args_piece) if args_piece else {}
            except json.JSONDecodeError:
                args = {}
            yield chunk({"candidates": [{"content": {"role": "model",
                       "parts": [{"functionCall": {"name": name or "tool", "args": args}}]},
                       "index": 0}], "modelVersion": model})
        elif kind == "usage":
            usage = {"promptTokenCount": ev[1], "candidatesTokenCount": ev[2]}
        elif kind == "end":
            finish = {"stop": "STOP", "tool_calls": "STOP",
                      "length": "MAX_TOKENS"}.get(ev[1], "STOP")
    yield chunk({"candidates": [{"content": {"role": "model", "parts": []},
                "finishReason": finish, "index": 0}],
                "usageMetadata": {**usage,
                "totalTokenCount": usage["promptTokenCount"] + usage["candidatesTokenCount"]},
                "modelVersion": model})
