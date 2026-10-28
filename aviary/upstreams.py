"""上游适配器:OpenAI 型与 Anthropic 型,统一为 (非流式响应, 流式事件迭代器)。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from .events import URequest
from .streaming import anthropic_sse_to_universe, openai_sse_to_universe


class UpstreamError(Exception):
    """上游不可用/报错,供路由器决定是否切换下一个成员。"""

    def __init__(self, status: int | None, detail: str):
        self.status = status
        super().__init__(f"upstream error {status}: {detail}")


@dataclass
class Provider:
    name: str
    type: str  # openai | anthropic
    base_url: str
    api_key: str = ""
    auth_header: str = "x-api-key"  # anthropic 用 x-api-key;部分中转用 Authorization
    timeout: float = 300.0
    models: list[str] | None = None
    price_in: float = 0.0   # $/1M 输入 token
    price_out: float = 0.0  # $/1M 输出 token
    extra_headers: dict | None = None

    def headers(self) -> dict:
        h: dict = {}
        if self.type == "anthropic":
            h["anthropic-version"] = "2007-01-25"  # 固定 API 版本,可被 extra_headers 覆盖
            h[self.auth_header] = self.api_key
            if self.auth_header != "Authorization":
                h["Authorization"] = f"Bearer {self.api_key}"
        else:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self.extra_headers:
            h.update(self.extra_headers)
        return h


def _sse_lines(resp: httpx.Response) -> AsyncIterator[str]:
    async def gen() -> AsyncIterator[str]:
        async for line in resp.aiter_lines():
            yield line
    return gen()


class Upstream:
    def __init__(self, provider: Provider):
        self.p = provider
        self._http = httpx.AsyncClient(
            base_url=provider.base_url.rstrip("/"), headers=provider.headers(),
            timeout=provider.timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------- non-stream
    async def complete(self, u: URequest) -> dict:
        """非流式:返回统一元数据 dict(text/tool_calls/finish_reason/usage)。"""
        from .translate import (anthropic_response_to_unified, unified_to_anthropic_request,
                                unified_to_openai_request, openai_response_to_unified)
        if self.p.type == "anthropic":
            body = unified_to_anthropic_request(u)
            data = await self._post("/v1/messages", body)
            return anthropic_response_to_unified(data)[1]
        body = unified_to_openai_request(u)
        body["stream"] = False
        data = await self._post("/chat/completions", body)
        return openai_response_to_unified(data)[1]

    # --------------------------------------------------------------- streaming
    async def stream(self, u: URequest) -> AsyncIterator[tuple]:
        """流式:yield 统一事件。出错时先抛 UpstreamError(未开始产出前可切换)。"""
        from .translate import unified_to_anthropic_request, unified_to_openai_request
        if self.p.type == "anthropic":
            body = unified_to_anthropic_request(u)
            path = "/v1/messages"
        else:
            body = unified_to_openai_request(u)
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            path = "/chat/completions"
        resp = await self._post_raw(path, body, stream=True)
        lines = _sse_lines(resp)
        if self.p.type == "anthropic":
            return anthropic_sse_to_universe(lines)
        return openai_sse_to_universe(lines)

    # ------------------------------------------------------------------ internals
    async def _post(self, path: str, body: dict) -> dict:
        resp = await self._post_raw(path, body, stream=False)
        return resp.json()

    async def _post_raw(self, path: str, body: dict, *, stream: bool) -> httpx.Response:
        try:
            resp = await self._http.post(path, json=body)
        except httpx.HTTPError as e:
            raise UpstreamError(None, f"{type(e).__name__}: {e}") from e
        if resp.status_code >= 400:
            detail = resp.text[:300]
            try:
                detail = json.dumps(resp.json(), ensure_ascii=False)[:300]
            except Exception:
                pass
            raise UpstreamError(resp.status_code, detail)
        return resp

    async def list_models(self) -> list[str]:
        if self.p.models is not None:
            return list(self.p.models)
        path = "/models" if self.p.type == "openai" else "/v1/models"
        try:
            data = (await self._post(path, {})).get("data") or []
            return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]
        except Exception:
            return []
