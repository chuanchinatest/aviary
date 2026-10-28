"""可独立运行的 mock 上游:同时提供 OpenAI 与 Anthropic 两种协议,便于本地演示。

  python mock_upstream.py            # OpenAI 于 :8850/v1, Anthropic 于 :8851
  python mock_upstream.py 8860 8861

行为:
  模型 mock-chat / claude-mock   —— 正常文本回复
  模型 mock-flaky                —— 非 200 状态码计数可控:前 N 次返回 500(N=失败次数,
                                    环境变量 FLAKY_N,默认 1),之后正常
  模型 mock-tool                 —— 回复一次工具调用(get_weather)
支持流式与非流式;流式输出分多个 SSE 片段。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def make_openai_app() -> FastAPI:
    app = FastAPI(docs_url=None)
    state = {"fail_left": int(os.environ.get("FLAKY_N", "1"))}

    def ok_response(model: str, stream: bool):
        text = f"来自 {model} 的问候:您好!"
        tool_call = None
        if model == "mock-tool":
            tool_call = [{"id": "call_1", "type": "function",
                          "function": {"name": "get_weather",
                                       "arguments": json.dumps({"city": "杭州"})}}]
        if not stream:
            msg = {"role": "assistant", "content": None if tool_call else text}
            if tool_call:
                msg["tool_calls"] = tool_call
            return JSONResponse({
                "id": "chatcmpl-x", "object": "chat.completion", "created": 0,
                "model": model,
                "choices": [{"index": 0, "message": msg,
                             "finish_reason": "tool_calls" if tool_call else "stop"}],
                "usage": {"prompt_tokens": 21, "completion_tokens": 13,
                          "total_tokens": 34}})

        async def gen():
            yield {"id": "chatcmpl-x", "object": "chat.completion.chunk", "created": 0,
                   "model": model, "choices": [{"index": 0,
                   "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
            if tool_call:
                yield {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": tool_call[0]["id"], "type": "function",
                     "function": {"name": tool_call[0]["function"]["name"],
                                  "arguments": ""}}]}, "finish_reason": None}]}
                yield {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": tool_call[0]["function"]["arguments"]}}]},
                    "finish_reason": None}]}
            else:
                for piece in [text[:4], text[4:8], text[8:]]:
                    yield {"choices": [{"index": 0, "delta": {"content": piece},
                                        "finish_reason": None}]}
            yield {"choices": [{"index": 0, "delta": {},
                                "finish_reason": "tool_calls" if tool_call else "stop"}]}
            yield {"choices": [], "usage": {"prompt_tokens": 21, "completion_tokens": 13}}
            yield "[DONE]"
        return gen

    @app.post("/v1/chat/completions")
    async def chat(body: dict, request: Request):
        model = body.get("model", "mock-chat")
        if model == "mock-flaky" and state["fail_left"] > 0:
            state["fail_left"] -= 1
            return JSONResponse({"error": "mock 临时故障"}, status_code=500)
        if body.get("stream"):
            async def sse_gen():
                async for p in ok_response(model, True)():
                    yield _sse(p) if isinstance(p, dict) else "data: [DONE]\n\n"
            return StreamingResponse(sse_gen(), media_type="text/event-stream")
        return ok_response(model, False)

    @app.get("/v1/models")
    async def models():
        return {"data": [{"id": m} for m in ["mock-chat", "mock-flaky", "mock-tool"]]}
    return app


def make_anthropic_app() -> FastAPI:
    app = FastAPI(docs_url=None)

    def blocks(model: str):
        if model.endswith("-tool"):
            return [{"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                     "input": {"city": "杭州"}}]
        return [{"type": "text", "text": f"来自 {model} 的回答:你好!"}]

    @app.post("/v1/messages")
    async def messages(body: dict, request: Request):
        model = body.get("model", "claude-mock")
        blocks_ = blocks(model)
        stop = "tool_use" if blocks_[0]["type"] == "tool_use" else "end_turn"
        if not body.get("stream"):
            return JSONResponse({
                "id": "msg-x", "type": "message", "role": "assistant", "model": model,
                "content": blocks_, "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 17, "output_tokens": 11}})
        return StreamingResponse(_anthropic_sse(model, blocks_, stop),
                                 media_type="text/event-stream")

    async def _anthropic_sse(model: str, blocks_: list, stop: str):
        def ev(name: str, payload: dict) -> str:
            return f"event: {name}\ndata: " + json.dumps({"type": name, **payload},
                                                         ensure_ascii=False) + "\n\n"
        yield ev("message_start", {"message": {"id": "msg-x", "type": "message",
               "role": "assistant", "model": model, "content": [], "stop_reason": None,
               "usage": {"input_tokens": 17, "output_tokens": 0}}})
        for i, b in enumerate(blocks_):
            if b["type"] == "text":
                yield ev("content_block_start",
                         {"index": i, "content_block": {"type": "text", "text": ""}})
                yield ev("content_block_delta", {"index": i,
                        "delta": {"type": "text_delta", "text": b["text"]}})
            else:
                yield ev("content_block_start",
                         {"index": i, "content_block": {"type": "tool_use",
                          "id": b["id"], "name": b["name"], "input": {}}})
                yield ev("content_block_delta", {"index": i,
                        "delta": {"type": "input_json_delta",
                                  "partial_json": json.dumps(b["input"], ensure_ascii=False)}})
            yield ev("content_block_stop", {"index": i})
        yield ev("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None},
                                   "usage": {"output_tokens": 11}})
        yield ev("message_stop", {})

    return app


def make_gemini_app() -> FastAPI:
    app = FastAPI(docs_url=None)

    @app.post("/v1beta/models/{model_path:path}")
    async def generate(model_path: str, body: dict, request: Request):
        model = model_path.split(":")[0]
        parts_out: list[dict] = []
        for c in body.get("contents") or []:
            for p in c.get("parts") or []:
                if "text" in p and p["text"]:
                    parts_out.append({"text": f"gemini-mock 收到:{p['text'][:20]}"})
        parts_out = parts_out or [{"text": f"来自 {model} 的回答:你好!"}]
        usage = {"promptTokenCount": 15, "candidatesTokenCount": 9}
        if model_path.endswith(":streamGenerateContent") or \
                request.query_params.get("alt") == "sse":
            async def sse():
                def ev(payload: dict) -> str:
                    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                for part in parts_out:
                    yield ev({"candidates": [{"content": {"role": "model",
                            "parts": [part]}, "index": 0}], "modelVersion": model})
                yield ev({"candidates": [{"content": {"role": "model", "parts": []},
                        "finishReason": "STOP", "index": 0}],
                        "usageMetadata": {**usage, "totalTokenCount": 24},
                        "modelVersion": model})
            return StreamingResponse(sse(), media_type="text/event-stream")
        return JSONResponse({"candidates": [{"content": {"role": "model",
                             "parts": parts_out}, "finishReason": "STOP", "index": 0}],
                             "usageMetadata": {**usage, "totalTokenCount": 24},
                             "modelVersion": model})
    return app


if __name__ == "__main__":
    openai_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8850
    anthropic_port = int(sys.argv[2]) if len(sys.argv) > 2 else 8851
    gemini_port = int(sys.argv[3]) if len(sys.argv) > 3 else 8852

    async def main():
        c1 = uvicorn.Server(uvicorn.Config(make_openai_app(), host="127.0.0.1",
                                           port=openai_port, log_level="warning"))
        c2 = uvicorn.Server(uvicorn.Config(make_anthropic_app(), host="127.0.0.1",
                                           port=anthropic_port, log_level="warning"))
        c3 = uvicorn.Server(uvicorn.Config(make_gemini_app(), host="127.0.0.1",
                                           port=gemini_port, log_level="warning"))
        await asyncio.gather(c1.serve(), c2.serve(), c3.serve())
    print(f"mock 上游:OpenAI :{openai_port}/v1, Anthropic :{anthropic_port},"
          f" Gemini :{gemini_port}/v1beta")
    asyncio.run(main())
