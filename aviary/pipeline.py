"""请求管线:一次调用要走的完整旅程,与 FastAPI 装配解耦。

鉴权 → 配额 → 中间件 → 路由 → 粘性 → 脱敏 → 逐候选故障切换 → 记账 → 响应。
网关(gateway.py)只负责把 HTTP 端点接到 pipeline.handle() 上。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .events import EventTap, URequest, chain
from .masking import Masker
from .middleware import apply_middlewares
from .protocols import ClientProtocol
from .protocols import get as get_protocol
from .routing import Router
from .stickiness import StickySessions
from .upstreams import Provider, Upstream, UpstreamError
from .usage import UsageLog, UsageRecord

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def presented_key(request: Request) -> str:
    """客户端出示的密钥:Authorization: Bearer <key> 优先,其次 x-api-key。"""
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return got or request.headers.get("x-api-key", "").strip()


def mask_request(masker: Masker, u: URequest) -> None:
    """请求出境前,把 system/正文/工具参数里的敏感值换成占位符。"""
    if u.system:
        u.system = masker.mask(u.system)
    for m in u.messages:
        if isinstance(m.get("content"), str) and m["content"]:
            m["content"] = masker.mask(m["content"])
        for tc in m.get("tool_calls") or []:
            args = tc["function"].get("arguments")
            if args:
                tc["function"]["arguments"] = masker.mask(args)


def restore_meta(masker: Masker, meta: dict) -> dict:
    """非流式回复回来后,把占位符还原为原文。"""
    meta = dict(meta)
    if meta.get("text"):
        meta["text"] = masker.restore(meta["text"])
    meta["tool_calls"] = [
        {**tc, "function": {**tc["function"],
                            "arguments": masker.restore(tc["function"].get("arguments") or "")}}
        for tc in meta.get("tool_calls") or []]
    return meta


class RequestPipeline:
    """持有网关全部依赖,handle() 是唯一的请求入口。"""

    def __init__(self, providers: list[Provider], router: Router,
                 usage_log: UsageLog | None, registry, masker: Masker,
                 sticky: StickySessions, middlewares: list[dict] | None):
        self.router = router
        self.usage_log = usage_log
        self.registry = registry
        self.masker = masker
        self.sticky = sticky
        self.middlewares = middlewares
        self.upstreams = {p.name: Upstream(p) for p in providers}

    async def handle(self, protocol: str, body: dict, request: Request,
                     requested: str | None = None) -> Response:
        proto: ClientProtocol = get_protocol(protocol)
        presented = presented_key(request)
        ok, reason, _name = self.registry.check(presented)
        if not ok:
            return JSONResponse({"error": {"type": "authentication_error",
                                           "message": reason}}, status_code=401)
        if self.usage_log:
            blocked = self.registry.over_quota(presented, self.usage_log)
            if blocked:
                return JSONResponse({"error": {"type": "quota_exceeded",
                                               "message": blocked}}, status_code=429)
        requested = requested or body.get("model") or ""
        body = apply_middlewares(self.middlewares, body, requested)
        try:
            resolution = self.router.resolve(requested)
        except KeyError as e:
            return JSONResponse({"error": {"type": "invalid_request_error",
                                           "message": str(e)}}, status_code=400)
        session = StickySessions.derive(request.headers.get("x-aviary-session"),
                                        requested, body.get("messages", []))
        resolution.candidates = self.sticky.reorder(session, resolution.candidates)

        stream = bool(body.get("stream"))
        rec = UsageRecord(requested_model=requested, protocol_in=protocol,
                          stream=stream, gateway_key=presented)
        start = time.time()
        last_error = "无可用上游"
        tried: list[str] = []

        for provider, upstream_model in resolution.candidates:
            tried.append(f"{provider.name}/{upstream_model}")
            up = self.upstreams[provider.name]
            u = proto.to_unified(body, upstream_model)
            if self.masker._values:
                mask_request(self.masker, u)
            try:
                if not stream:
                    meta = await up.complete(u)
                    if self.masker:
                        meta = restore_meta(self.masker, meta)
                    self._finish_ok(rec, provider, upstream_model, meta,
                                    tried, start, session)
                    return JSONResponse(proto.from_meta(meta, upstream_model))
                return await self._stream_reply(proto, up, u, provider,
                                                upstream_model, rec,
                                                tried, start, session)
            except UpstreamError as e:
                last_error = str(e)
                if e.status and e.status < 500 and e.status != 429:
                    return self._client_error(rec, e.status, last_error, tried)
                continue
        rec.status, rec.error, rec.attempts = "error", last_error, len(tried)
        if self.usage_log:
            self.usage_log.write(rec)
        return JSONResponse({"error": {"type": "upstream_error",
                                       "message": f"所有上游失败: {last_error}"}},
                            status_code=502)

    async def _stream_reply(self, proto: ClientProtocol, up: Upstream,
                            u: URequest, provider: Provider, upstream_model: str,
                            rec: UsageRecord, tried: list[str], start: float,
                            session: str) -> Response:
        """流式:先取第一个事件(失败可切下一候选),再向客户端产出。"""
        events = await up.stream(u)
        try:
            first = await events.__anext__()
        except UpstreamError as e:
            if e.status and e.status < 500 and e.status != 429:
                return self._client_error(rec, e.status, str(e), tried)
            raise  # 429/5xx/未知:交给外层循环换下一候选
        except StopAsyncIteration:
            raise UpstreamError(None, f"{provider.name}: 上游未产出任何事件") from None
        tap = EventTap(chain(first, events))
        events_for_client: AsyncIterator[tuple] = tap
        if self.masker and self.masker._values:
            events_for_client = self.masker.restore_stream(events_for_client)

        async def guarded() -> AsyncIterator[str]:
            try:
                async for chunk in proto.to_sse(events_for_client, upstream_model):
                    yield chunk
            finally:
                self._finish_ok(rec, provider, upstream_model,
                                {"usage": tap.usage, "finish_reason": tap.finish},
                                tried, start, session)
        return StreamingResponse(guarded(), media_type="text/event-stream",
                                 headers=SSE_HEADERS)

    def _client_error(self, rec: UsageRecord, status: int, message: str,
                      tried: list[str]) -> JSONResponse:
        rec.status, rec.error, rec.attempts = "client_error", message, len(tried)
        if self.usage_log:
            self.usage_log.write(rec)
        return JSONResponse({"error": {"type": "upstream_error", "message": message}},
                            status_code=status)

    def _finish_ok(self, rec: UsageRecord, provider: Provider, upstream_model: str,
                   meta: dict, tried: list[str], start: float, session: str) -> None:
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
        self.router.record_usage(rec.requested_model,
                                 f"{provider.name}/{upstream_model}")
        self.sticky.set(session, (provider.name, upstream_model))
        if self.usage_log:
            self.usage_log.write(rec)
