"""FastAPI 网关:/v1/chat/completions(OpenAI)、/v1/messages(Anthropic)、/v1/models。

所有请求流程:鉴权 → 解析模型寻址 → 逐个候选上游尝试 → 协议翻译 → 记一次用量
(含故障切换)。流式请求在真正向客户端产出之前,上游失败仍可切换到下一个候选。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .events import URequest
from .routing import Router
from .streaming import (universe_to_anthropic_sse, universe_to_openai_sse)
from .translate import (anthropic_request_to_unified, openai_request_to_unified,
                        unified_to_anthropic_response, unified_to_openai_response)
from .upstreams import Provider, Upstream, UpstreamError
from .usage import UsageLog, UsageRecord


class _Tap:
    """包一层事件流,顺路收集 finish_reason 与 usage 供记账。"""

    def __init__(self, it: AsyncIterator[tuple]):
        self._it = it
        self.finish = "stop"
        self.usage: dict = {}

    def __aiter__(self) -> "_Tap":
        return self

    async def __anext__(self) -> tuple:
        ev = await self._it.__anext__()
        if ev[0] == "usage":
            self.usage = {"prompt_tokens": ev[1], "completion_tokens": ev[2]}
        elif ev[0] == "end":
            self.finish = ev[1]
        return ev


def create_app(providers: list[Provider], router: Router, usage_log: UsageLog | None,
               api_key: str = "") -> FastAPI:
    app = FastAPI(title="aviary", docs_url=None, redoc_url=None)
    upstreams = {p.name: Upstream(p) for p in providers}

    def authorized(request: Request) -> bool:
        if not api_key:
            return True
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        got = got or request.headers.get("x-api-key", "")
        return got == api_key

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "version": "0.1.0"})

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        ids: list[str] = list(router.model_map)
        for name, up in upstreams.items():
            for m in await up.list_models():
                ids.append(f"{name}/{m}")
        return JSONResponse({"object": "list",
                             "data": [{"id": m, "object": "model", "owned_by": "aviary"}
                                      for m in ids]})

    async def run(protocol: str, body: dict, request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": {"type": "authentication_error",
                                           "message": "无效的网关密钥"}}, status_code=401)
        requested = body.get("model") or ""
        try:
            resolution = router.resolve(requested)
        except KeyError as e:
            return JSONResponse({"error": {"type": "invalid_request_error",
                                           "message": str(e)}}, status_code=400)
        stream = bool(body.get("stream"))
        rec = UsageRecord(requested_model=requested, protocol_in=protocol, stream=stream)
        start = time.time()
        last_error = "无可用上游"
        tried: list[str] = []

        for provider, upstream_model in resolution.candidates:
            tried.append(f"{provider.name}/{upstream_model}")
            up = upstreams[provider.name]
            u = _unified(protocol, body, upstream_model)
            try:
                if not stream:
                    meta = await up.complete(u)
                    _finish_ok(rec, provider, upstream_model, meta, tried, start,
                               router, requested, usage_log)
                    return _respond(protocol, meta, upstream_model)
                # 流式:建立连接并拿到首个事件,失败/4xx 仍可切换
                events = await up.stream(u)
                try:
                    first = await events.__anext__()
                except UpstreamError as e:
                    last_error = str(e)
                    if e.status and e.status < 500 and e.status != 429:
                        return _client_error(rec, e.status, last_error, tried, usage_log)
                    continue
                except StopAsyncIteration:
                    last_error = f"{provider.name}: 上游未产出任何事件"
                    continue
                router.record_usage(requested, f"{provider.name}/{upstream_model}")
                tap = _Tap(_chain(first, events))
                member = f"{provider.name}/{upstream_model}"

                async def guarded() -> AsyncIterator[str]:
                    try:
                        if protocol == "openai":
                            async for chunk in universe_to_openai_sse(tap, upstream_model):
                                yield chunk
                        else:
                            async for chunk in universe_to_anthropic_sse(tap, upstream_model):
                                yield chunk
                    finally:
                        _finish_ok(rec, provider, upstream_model,
                                   {"usage": tap.usage, "finish_reason": tap.finish},
                                   tried, start, router, requested, usage_log)
                return StreamingResponse(
                    guarded(), media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            except UpstreamError as e:
                last_error = str(e)
                if e.status and e.status < 500 and e.status != 429:
                    return _client_error(rec, e.status, last_error, tried, usage_log)
                continue
        rec.status, rec.error, rec.attempts = "error", last_error, len(tried)
        if usage_log:
            usage_log.write(rec)
        return JSONResponse({"error": {"type": "upstream_error",
                                       "message": f"所有上游失败: {last_error}"}},
                            status_code=502)

    @app.post("/v1/chat/completions")
    async def chat(body: dict, request: Request) -> Response:
        return await run("openai", body, request)

    @app.post("/v1/messages")
    async def messages(body: dict, request: Request) -> Response:
        return await run("anthropic", body, request)

    return app


async def _chain(first: tuple, rest: AsyncIterator[tuple]) -> AsyncIterator[tuple]:
    yield first
    async for ev in rest:
        yield ev


def _unified(protocol: str, body: dict, upstream_model: str) -> URequest:
    if protocol == "openai":
        return openai_request_to_unified(body, upstream_model)
    return anthropic_request_to_unified(body, upstream_model)


def _respond(protocol: str, meta: dict, upstream_model: str) -> JSONResponse:
    if protocol == "openai":
        return JSONResponse(unified_to_openai_response(meta, upstream_model))
    return JSONResponse(unified_to_anthropic_response(meta, upstream_model))


def _client_error(rec: UsageRecord, status: int, message: str,
                  tried: list[str], usage_log: UsageLog | None) -> JSONResponse:
    rec.status, rec.error, rec.attempts = "client_error", message, len(tried)
    if usage_log:
        usage_log.write(rec)
    return JSONResponse({"error": {"type": "upstream_error", "message": message}},
                        status_code=status)


def _finish_ok(rec: UsageRecord, provider: Provider, upstream_model: str, meta: dict,
               tried: list[str], start: float, router: Router, requested: str,
               usage_log: UsageLog | None) -> None:
    usage = meta.get("usage") or {}
    rec.provider, rec.upstream_model = provider.name, upstream_model
    rec.protocol_out = provider.type
    rec.status, rec.attempts = "ok", len(tried)
    rec.latency_ms = int((time.time() - start) * 1000)
    rec.prompt_tokens = usage.get("prompt_tokens", 0)
    rec.completion_tokens = usage.get("completion_tokens", 0)
    if provider.price_in or provider.price_out:
        rec.cost_usd = round((rec.prompt_tokens * provider.price_in
                              + rec.completion_tokens * provider.price_out) / 1e6, 6)
    router.record_usage(requested, f"{provider.name}/{upstream_model}")
    if usage_log:
        usage_log.write(rec)
