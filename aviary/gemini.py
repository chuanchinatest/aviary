"""Gemini 协议:客户端请求/响应格式 ↔ 统一模型;Gemini 型上游的请求构造与事件解析。

端点(GET /v1beta/models):
  POST /v1beta/models/{model}:generateContent           非流式
  POST /v1beta/models/{model}:streamGenerateContent?alt=sse   流式
"""

from __future__ import annotations

import json

from .events import FINISH_MAP_TO_OPENAI, URequest


# ---------------------------------------------------------------------------
# Gemini 请求 → 统一请求
# ---------------------------------------------------------------------------
def gemini_request_to_unified(body: dict, model: str) -> URequest:
    msgs: list[dict] = []
    for c in body.get("contents") or []:
        role = "assistant" if c.get("role") == "model" else "user"
        parts = c.get("parts") or []
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []
        for p in parts:
            if "text" in p:
                text_parts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append({
                    "id": fc.get("name", "call"), "type": "function",
                    "function": {"name": fc["name"],
                                 "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False)},
                })
            elif "functionResponse" in p:
                fr = p["functionResponse"]
                tool_results.append({
                    "tool_call_id": fr.get("name", "call"),
                    "role": "tool",
                    "content": json.dumps(fr.get("response", {}), ensure_ascii=False),
                })
        if tool_results:
            msgs.extend(tool_results)
        else:
            m: dict = {"role": role, "content": "".join(text_parts)}
            if tool_calls:
                m["tool_calls"] = tool_calls
                m["content"] = "".join(text_parts) or None
            msgs.append(m)
    tools = None
    for t in body.get("tools") or []:
        decls = t.get("functionDeclarations") or []
        if decls:
            tools = [{"name": d["name"], "description": d.get("description", ""),
                      "parameters": d.get("parameters") or {"type": "object"}} for d in decls]
    sys = None
    si = body.get("systemInstruction") or body.get("system_instruction")
    if si:
        sys = "".join(p.get("text", "") for p in si.get("parts") or [])
    gc = body.get("generationConfig") or {}
    return URequest(
        model=model, messages=msgs, system=sys, tools=tools,
        max_tokens=gc.get("maxOutputTokens") or 4096,
        temperature=gc.get("temperature"),
        top_p=gc.get("topP"),
        stream=bool(body.get("_stream") or body.get("stream")),
        stop=gc.get("stopSequences"),
        raw=body,
    )


def unified_to_gemini_request(u: URequest) -> dict:
    contents: list[dict] = []
    for m in u.messages:
        role = "assistant" if m.get("role") == "assistant" else "user"
        if m.get("role") == "tool":
            try:
                resp = json.loads(m.get("content") or "{}")
            except json.JSONDecodeError:
                resp = {"result": m.get("content") or ""}
            contents.append({"role": "user", "parts": [
                {"functionResponse": {"name": m.get("tool_call_id") or "tool",
                                      "response": resp}}]})
            continue
        parts: list[dict] = []
        if m.get("content"):
            parts.append({"text": m["content"]})
        for tc in m.get("tool_calls") or []:
            args = tc["function"].get("arguments") or "{}"
            parts.append({"functionCall": {"name": tc["function"]["name"],
                          "args": json.loads(args) if isinstance(args, str) and args.strip() else (args or {})}})
        contents.append({"role": role, "parts": parts or [{"text": ""}]})
    body: dict = {"contents": contents}
    if u.system:
        body["systemInstruction"] = {"parts": [{"text": u.system}]}
    gc: dict = {"maxOutputTokens": u.max_tokens}
    if u.temperature is not None:
        gc["temperature"] = u.temperature
    if u.top_p is not None:
        gc["topP"] = u.top_p
    if u.stop:
        gc["stopSequences"] = u.stop
    body["generationConfig"] = gc
    if u.tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t.get("description", ""),
             "parameters": t.get("parameters") or {"type": "object"}} for t in u.tools]}]
    return body


# ---------------------------------------------------------------------------
# 统一元数据 → Gemini 响应
# ---------------------------------------------------------------------------
def unified_to_gemini_response(meta: dict, model: str) -> dict:
    parts: list[dict] = []
    if meta.get("text"):
        parts.append({"text": meta["text"]})
    for tc in meta.get("tool_calls") or []:
        args = tc["function"].get("arguments") or "{}"
        parts.append({"functionCall": {"name": tc["function"]["name"],
                      "args": json.loads(args) if isinstance(args, str) and args.strip() else (args or {})}})
    usage = meta.get("usage") or {}
    finish = {"stop": "STOP", "tool_calls": "STOP", "length": "MAX_TOKENS"}.get(
        meta.get("finish_reason") or "stop", "STOP")
    return {
        "candidates": [{"content": {"role": "model", "parts": parts or [{"text": ""}]},
                        "finishReason": finish, "index": 0}],
        "usageMetadata": {"promptTokenCount": usage.get("prompt_tokens", 0),
                          "candidatesTokenCount": usage.get("completion_tokens", 0),
                          "totalTokenCount": usage.get("prompt_tokens", 0)
                          + usage.get("completion_tokens", 0)},
        "modelVersion": model,
    }


def gemini_response_to_unified(data: dict) -> tuple[str, dict]:
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for p in parts:
        if "text" in p:
            text_parts.append(p["text"])
        elif "functionCall" in p:
            fc = p["functionCall"]
            tool_calls.append({"id": fc.get("name", "call"), "type": "function",
                               "function": {"name": fc["name"],
                               "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False)}})
    usage = data.get("usageMetadata") or {}
    meta = {
        "text": "".join(text_parts), "tool_calls": tool_calls,
        "finish_reason": FINISH_MAP_TO_OPENAI.get(
            {"STOP": "stop", "MAX_TOKENS": "max_tokens"}.get(cand.get("finishReason") or "STOP", "stop"),
            "stop"),
        "usage": {"prompt_tokens": usage.get("promptTokenCount", 0),
                  "completion_tokens": usage.get("candidatesTokenCount", 0)},
    }
    return meta["text"], meta
