"""FastAPI 网关:四种客户端协议 × 三类上游,统一寻址、粘性、脱敏、配额、中间件。

客户端协议:
  POST /v1/chat/completions            OpenAI Chat
  POST /v1/messages                    Anthropic Messages
  POST /v1/responses                   OpenAI Responses
  POST /v1beta/models/{model}:generateContent(及 :streamGenerateContent)  Gemini
管理:
  GET  /healthz  /v1/models  /stats  /(仪表盘)
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .events import URequest
from .gemini import gemini_request_to_unified, unified_to_gemini_response
from .gemini_stream import universe_to_gemini_sse
from .masking import Masker
from .middleware import apply_middlewares
from .responses_api import (responses_request_to_unified,
                            unified_to_responses_response,
                            universe_to_responses_sse)
from .routing import Router
from .stickiness import StickySessions
from .streaming import universe_to_anthropic_sse, universe_to_openai_sse
from .translate import (anthropic_request_to_unified, openai_request_to_unified,
                        unified_to_anthropic_response, unified_to_openai_response)
from .upstreams import Provider, Upstream, UpstreamError
from .usage import UsageLog, UsageRecord

DASHBOARD = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>aviary 控制台</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{font-family:ui-sans-serif,system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
      background:#0f1117;color:#e6e6eb;margin:0;padding:2rem}
 h1{font-size:1.3rem} h1 span{color:#8ab4ff}
 table{border-collapse:collapse;width:100%;margin-top:1rem;font-size:.92rem}
 th,td{border:1px solid #2a2d3a;padding:.45rem .7rem;text-align:left}
 th{background:#161926;color:#9aa3b5}
 .kpi{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}
 .kpi div{background:#161926;border:1px solid #2a2d3a;border-radius:.6rem;padding:.8rem 1.2rem}
 .kpi b{display:block;font-size:1.4rem;color:#8ab4ff}
 code{color:#9ece6a}
</style></head><body>
<h1><span>aviary</span> · 每个模型,一个入口</h1>
<div class="kpi" id="kpi"></div>
<table id="tbl"><thead><tr><th>请求模型</th><th>次数</th><th>输入 tok</th>
<th>输出 tok</th><th>成本 $</th></tr></thead><tbody></tbody></table>
<p style="color:#9aa3b5;font-size:.85rem">端点:<code>/v1/chat/completions</code> ·
<code>/v1/messages</code> · <code>/v1/responses</code> ·
<code>/v1beta/models/&lt;model&gt;:generateContent</code></p>
<script>
fetch('/stats').then(r=>r.json()).then(s=>{
 const k=document.getElementById('kpi');
 k.innerHTML=`<div>总请求<b>${s.total_requests}</b></div>
  <div>成功<b>${s.ok}</b></div><div>输入<b>${s.prompt_tokens}</b></div>
  <div>输出<b>${s.completion_tokens}</b></div><div>成本<b>$${s.cost_usd.toFixed(6)}</b></div>`;
 const tb=document.querySelector('#tbl tbody');
 tb.innerHTML=Object.entries(s.by_model).map(([m,v])=>
  `<tr><td><code>${m}</code></td><td>${v.requests}</td><td>${v.prompt_tokens}</td>
   <td>${v.completion_tokens}</td><td>${v.cost_usd.toFixed(6)}</td></tr>`).join('');
});
</script></body></html>"""


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


def create_app(providers: list[Provider], router: Router,
               usage_log: UsageLog | None = None, *, api_key: str = "",
               keys=None, masker: Masker | None = None,
               middlewares: list[dict] | None = None,
               sticky: StickySessions | None = None) -> FastAPI:
    from .limits import KeyRegistry
    app = FastAPI(docs_url=None, redoc_url=None)
    upstreams = {p.name: Upstream(p) for p in providers}
    registry = keys if keys is not None else KeyRegistry([], api_key=api_key)
    masker = masker or Masker()
    sticky = sticky or StickySessions()

    def _presented(request: Request) -> str:
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return got or request.headers.get("x-api-key", "").strip()

    def _sse_translate(protocol: str, events, model: str) -> AsyncIterator[str]:
        if protocol == "openai":
            return universe_to_openai_sse(events, model)
        if protocol == "anthropic":
            return universe_to_anthropic_sse(events, model)
        if protocol == "gemini":
            return universe_to_gemini_sse(events, model)
        return universe_to_responses_sse(events, model)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "version": "0.2.0"})

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD)

    @app.get("/stats")
    async def stats() -> JSONResponse:
        return JSONResponse(usage_log.summarize() if usage_log else {})

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        ok, _, _ = registry.check(_presented(request))
        if not ok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        ids: list[str] = list(router.model_map)
        for name, up in upstreams.items():
            for m in await up.list_models():
                ids.append(f"{name}/{m}")
        return JSONResponse({"object": "list",
                             "data": [{"id": m, "object": "model", "owned_by": "aviary"}
                                      for m in ids]})

    async def run(protocol: str, body: dict, request: Request,
                  requested: str | None = None) -> Response:
        presented = _presented(request)
        ok, reason, _name = registry.check(presented)
        if not ok:
            return JSONResponse({"error": {"type": "authentication_error",
                                           "message": reason}}, status_code=401)
        if usage_log:
            blocked = registry.over_quota(presented, usage_log)
            if blocked:
                return JSONResponse({"error": {"type": "quota_exceeded",
                                               "message": blocked}}, status_code=429)
        requested = requested or body.get("model") or ""
        body = apply_middlewares(middlewares, body, requested)
        try:
            resolution = router.resolve(requested)
        except KeyError as e:
            return JSONResponse({"error": {"type": "invalid_request_error",
                                           "message": str(e)}}, status_code=400)
        session = StickySessions.derive(request.headers.get("x-aviary-session"),
                                        requested, body.get("messages", []))
        resolution.candidates = sticky.reorder(session, resolution.candidates)

        stream = bool(body.get("stream"))
        rec = UsageRecord(requested_model=requested, protocol_in=protocol,
                          stream=stream, gateway_key=presented)
        start = time.time()
        last_error = "无可用上游"
        tried: list[str] = []

        for provider, upstream_model in resolution.candidates:
            tried.append(f"{provider.name}/{upstream_model}")
            up = upstreams[provider.name]
            u = _unified(protocol, body, upstream_model)
            if masker and masker._values:
                _mask_request(masker, u)
            try:
                if not stream:
                    meta = await up.complete(u)
                    if masker:
                        meta = _restore_meta(masker, meta)
                    _finish_ok(rec, provider, upstream_model, meta, tried, start,
                               router, requested, usage_log, sticky, session)
                    return _respond(protocol, meta, upstream_model)
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
                tap = _Tap(_chain(first, events))
                events_for_client: AsyncIterator[tuple] = tap
                if masker and masker._values:
                    events_for_client = masker.restore_stream(events_for_client)

                async def guarded() -> AsyncIterator[str]:
                    try:
                        async for chunk in _sse_translate(protocol, events_for_client,
                                                          upstream_model):
                            yield chunk
                    finally:
                        _finish_ok(rec, provider, upstream_model,
                                   {"usage": tap.usage, "finish_reason": tap.finish},
                                   tried, start, router, requested, usage_log,
                                   sticky, session)
                return StreamingResponse(guarded(), media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache",
                                                  "X-Accel-Buffering": "no"})
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

    @app.post("/v1/responses")
    async def responses_ep(body: dict, request: Request) -> Response:
        return await run("responses", body, request)

    @app.post("/v1beta/models/{model_path:path}")
    async def gemini_ep(model_path: str, body: dict, request: Request) -> Response:
        model = model_path.split(":")[0]
        stream = model_path.endswith(":streamGenerateContent") \
            or request.query_params.get("alt") == "sse"
        body = dict(body)
        body["stream"] = stream
        return await run("gemini", body, request, requested=model)

    return app


async def _chain(first: tuple, rest: AsyncIterator[tuple]) -> AsyncIterator[tuple]:
    yield first
    async for ev in rest:
        yield ev


def _unified(protocol: str, body: dict, upstream_model: str) -> URequest:
    if protocol == "openai":
        return openai_request_to_unified(body, upstream_model)
    if protocol == "anthropic":
        return anthropic_request_to_unified(body, upstream_model)
    if protocol == "responses":
        return responses_request_to_unified(body, upstream_model)
    return gemini_request_to_unified(body, upstream_model)


def _mask_request(masker: Masker, u: URequest) -> None:
    if u.system:
        u.system = masker.mask(u.system)
    for m in u.messages:
        if isinstance(m.get("content"), str) and m["content"]:
            m["content"] = masker.mask(m["content"])
        for tc in m.get("tool_calls") or []:
            args = tc["function"].get("arguments")
            if args:
                tc["function"]["arguments"] = masker.mask(args)


def _restore_meta(masker: Masker, meta: dict) -> dict:
    meta = dict(meta)
    if meta.get("text"):
        meta["text"] = masker.restore(meta["text"])
    meta["tool_calls"] = [
        {**tc, "function": {**tc["function"],
                            "arguments": masker.restore(tc["function"].get("arguments") or "")}}
        for tc in meta.get("tool_calls") or []]
    return meta


def _respond(protocol: str, meta: dict, upstream_model: str) -> JSONResponse:
    if protocol == "openai":
        return JSONResponse(unified_to_openai_response(meta, upstream_model))
    if protocol == "anthropic":
        return JSONResponse(unified_to_anthropic_response(meta, upstream_model))
    if protocol == "responses":
        return JSONResponse(unified_to_responses_response(meta, upstream_model))
    return JSONResponse(unified_to_gemini_response(meta, upstream_model))


def _client_error(rec: UsageRecord, status: int, message: str,
                  tried: list[str], usage_log: UsageLog | None) -> JSONResponse:
    rec.status, rec.error, rec.attempts = "client_error", message, len(tried)
    if usage_log:
        usage_log.write(rec)
    return JSONResponse({"error": {"type": "upstream_error", "message": message}},
                        status_code=status)


def _finish_ok(rec: UsageRecord, provider: Provider, upstream_model: str, meta: dict,
               tried: list[str], start: float, router: Router, requested: str,
               usage_log: UsageLog | None, sticky: StickySessions,
               session: str) -> None:
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
    sticky.set(session, (provider.name, upstream_model))
    if usage_log:
        usage_log.write(rec)
