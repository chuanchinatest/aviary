"""客户端协议注册表:每种协议的 解析、回包、SSE 转换,注册即用。

新增一种客户端协议(比如未来的 Bedrock 事件流)只需:
  1. 实现三个函数:xxx_request_to_unified / unified_to_xxx_response / universe_to_xxx_sse
  2. 在 PROTOCOLS 里注册一行
gateway 与 pipeline 不再需要任何 if/elif。

非流式响应统一为 (meta, upstream_model) -> 响应体 dict;
流式统一为 (统一事件流, upstream_model) -> SSE 字符串流。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from .events import URequest
from .gemini import gemini_request_to_unified, unified_to_gemini_response
from .gemini_stream import universe_to_gemini_sse
from .responses_api import (
    responses_request_to_unified,
    unified_to_responses_response,
    universe_to_responses_sse,
)
from .streaming import universe_to_anthropic_sse, universe_to_openai_sse
from .translate import (
    anthropic_request_to_unified,
    openai_request_to_unified,
    unified_to_anthropic_response,
    unified_to_openai_response,
)


@dataclass(frozen=True)
class ClientProtocol:
    """一种客户端协议的三个方向。"""

    name: str
    to_unified: Callable[[dict, str], URequest]          # (请求体, 上游模型名) -> URequest
    from_meta: Callable[[dict, str], dict]               # (统一元数据, 上游模型名) -> 响应体
    to_sse: Callable[[AsyncIterator[tuple], str], AsyncIterator[str]]


PROTOCOLS: dict[str, ClientProtocol] = {
    "openai": ClientProtocol(
        "openai", openai_request_to_unified,
        unified_to_openai_response, universe_to_openai_sse),
    "anthropic": ClientProtocol(
        "anthropic", anthropic_request_to_unified,
        unified_to_anthropic_response, universe_to_anthropic_sse),
    "responses": ClientProtocol(
        "responses", responses_request_to_unified,
        unified_to_responses_response, universe_to_responses_sse),
    "gemini": ClientProtocol(
        "gemini", gemini_request_to_unified,
        unified_to_gemini_response, universe_to_gemini_sse),
}


def get(name: str) -> ClientProtocol:
    try:
        return PROTOCOLS[name]
    except KeyError as e:
        raise ValueError(f"未知客户端协议: {name} (可用: {', '.join(PROTOCOLS)})") from e
