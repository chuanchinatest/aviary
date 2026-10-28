"""上游适配器:三类上游(openai/anthropic/gemini),适配表 + 统一 HTTP 客户端。

每类上游提供四个能力,登记在 ADAPTERS 里:
  complete(up, u)       非流式:返回统一元数据 dict
  stream(up, u)         流式:返回统一事件迭代器
  models_path           模型发现路径(None = 走 gemini 专属逻辑)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from .events import URequest
from .gemini import (
    gemini_response_to_unified,
    unified_to_gemini_request,
)
from .gemini_stream import gemini_sse_to_universe
from .streaming import anthropic_sse_to_universe, openai_sse_to_universe
from .translate import (
    anthropic_response_to_unified,
    openai_response_to_unified,
    unified_to_anthropic_request,
    unified_to_openai_request,
)


class UpstreamError(Exception):
    """上游不可用/报错,供路由器决定是否切换下一个成员。"""

    def __init__(self, status: int | None, detail: str):
        self.status = status
        super().__init__(f"upstream error {status}: {detail}")


@dataclass
class Provider:
    name: str
    type: str  # openai | anthropic | gemini
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
        if self.type == "gemini":
            h["x-goog-api-key"] = self.api_key
        elif self.type == "anthropic":
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


# --------------------------------------------------------------- 上游适配注册表
async def _openai_complete(up: Upstream, u: URequest) -> dict:
    body = unified_to_openai_request(u)
    body["stream"] = False
    data = await up._post("/chat/completions", body)
    return openai_response_to_unified(data)[1]


async def _openai_stream(up: Upstream, u: URequest) -> AsyncIterator[tuple]:
    body = unified_to_openai_request(u)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    resp = await up._post_raw("/chat/completions", body, stream=True)
    return openai_sse_to_universe(_sse_lines(resp))


async def _anthropic_complete(up: Upstream, u: URequest) -> dict:
    body = unified_to_anthropic_request(u)
    data = await up._post("/v1/messages", body)
    return anthropic_response_to_unified(data)[1]


async def _anthropic_stream(up: Upstream, u: URequest) -> AsyncIterator[tuple]:
    body = unified_to_anthropic_request(u)
    body["stream"] = True
    resp = await up._post_raw("/v1/messages", body, stream=True)
    return anthropic_sse_to_universe(_sse_lines(resp))


async def _gemini_complete(up: Upstream, u: URequest) -> dict:
    body = unified_to_gemini_request(u)
    data = await up._post(f"/v1beta/models/{u.model}:generateContent", body)
    return gemini_response_to_unified(data)[1]


async def _gemini_stream(up: Upstream, u: URequest) -> AsyncIterator[tuple]:
    body = unified_to_gemini_request(u)
    resp = await up._post_raw(
        f"/v1beta/models/{u.model}:streamGenerateContent?alt=sse", body, stream=True)
    return gemini_sse_to_universe(_sse_lines(resp))


ADAPTERS: dict[str, dict] = {
    "openai": {"complete": _openai_complete, "stream": _openai_stream,
               "models_path": "/models"},
    "anthropic": {"complete": _anthropic_complete, "stream": _anthropic_stream,
                  "models_path": "/v1/models"},
    "gemini": {"complete": _gemini_complete, "stream": _gemini_stream,
               "models_path": None},
}


class Upstream:
    def __init__(self, provider: Provider):
        self.p = provider
        if provider.type not in ADAPTERS:
            raise ValueError(f"未知上游类型: {provider.type} "
                             f"(可用: {', '.join(ADAPTERS)})")
        self._http = httpx.AsyncClient(
            base_url=provider.base_url.rstrip("/"), headers=provider.headers(),
            timeout=provider.timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------- non-stream
    async def complete(self, u: URequest) -> dict:
        """非流式:返回统一元数据 dict(text/tool_calls/finish_reason/usage)。"""
        return await ADAPTERS[self.p.type]["complete"](self, u)

    # --------------------------------------------------------------- streaming
    async def stream(self, u: URequest) -> AsyncIterator[tuple]:
        """流式:yield 统一事件。出错时先抛 UpstreamError(未开始产出前可切换)。"""
        return await ADAPTERS[self.p.type]["stream"](self, u)

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
        adapter = ADAPTERS[self.p.type]
        if adapter["models_path"] is None:  # gemini:列表结构不同
            try:
                data = await self._get("/v1beta/models")
                return [m["name"].split("/")[-1] for m in data.get("models", [])
                        if m.get("name")]
            except Exception:
                return []
        try:
            data = (await self._post(adapter["models_path"], {})).get("data") or []
            return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]
        except Exception:
            return []

    async def _get(self, path: str) -> dict:
        try:
            resp = await self._http.get(path)
        except httpx.HTTPError as e:
            raise UpstreamError(None, f"{type(e).__name__}: {e}") from e
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text[:300])
        return resp.json()
