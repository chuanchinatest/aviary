"""配置加载:YAML 或 JSON。

字段见 config.example.yaml;api_key、price 等敏感项支持环境变量插值 ${VAR}。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .masking import Masker
from .routing import Router
from .stickiness import StickySessions
from .upstreams import Provider
from .usage import UsageLog

_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _interp(v):
    if isinstance(v, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _interp(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_interp(x) for x in v]
    return v


def load_raw(path: str) -> dict:
    text = open(path, encoding="utf-8").read()
    if path.endswith((".yaml", ".yml")):
        import yaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return _interp(data or {})


def providers_from(raw: dict) -> list[Provider]:
    out = []
    for p in raw.get("providers") or []:
        out.append(Provider(
            name=p["name"],
            type=p.get("type", "openai"),  # openai | anthropic | gemini
            base_url=p["base_url"],
            api_key=p.get("api_key", ""),
            auth_header=p.get("auth", "x-api-key"),
            timeout=float(p.get("timeout", 300)),
            models=p.get("models"),
            price_in=float((p.get("price") or {}).get("input", 0)),
            price_out=float((p.get("price") or {}).get("output", 0)),
            extra_headers=p.get("extra_headers"),
        ))
    return out


def router_from(raw: dict, providers: list[Provider]) -> Router:
    return Router(providers, raw.get("groups") or [],
                  model_map=raw.get("model_map") or {},
                  default_provider=raw.get("gateway", {}).get("default_provider"))


def usage_log_from(raw: dict) -> UsageLog | None:
    log = raw.get("log") or {}
    path = log.get("file")
    return UsageLog(path) if path else None


def keys_from(raw: dict):
    from .limits import KeyRegistry
    g = raw.get("gateway") or {}
    return KeyRegistry(g.get("keys") or [], api_key=g.get("api_key", ""))


def masker_from(raw: dict) -> Masker:
    m = raw.get("masking") or {}
    return Masker(extra_patterns=m.get("patterns"), never_mask=m.get("never_mask"))


def sticky_from(raw: dict) -> StickySessions:
    s = raw.get("stickiness") or {}
    return StickySessions(ttl_seconds=float(s.get("ttl_seconds", 1800)))


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    api_key: str = ""


def gateway_from(raw: dict) -> GatewayConfig:
    g = raw.get("gateway") or {}
    return GatewayConfig(host=g.get("host", "127.0.0.1"),
                         port=int(g.get("port", 8787)),
                         api_key=g.get("api_key", ""))
