"""FastAPI 网关:端点装配层。业务逻辑在 pipeline,协议在 protocols,页面在 dashboard。

客户端协议:
  POST /v1/chat/completions            OpenAI Chat
  POST /v1/messages                    Anthropic Messages
  POST /v1/responses                   OpenAI Responses
  POST /v1beta/models/{model}:generateContent(及 :streamGenerateContent)  Gemini
管理:
  GET  /healthz  /v1/models  /stats  /(仪表盘)
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import __version__
from .dashboard import DASHBOARD_HTML
from .limits import KeyRegistry
from .masking import Masker
from .pipeline import RequestPipeline, presented_key
from .routing import Router
from .stickiness import StickySessions
from .usage import UsageLog

__all__ = ["create_app"]


def create_app(providers: list, router: Router,
               usage_log: UsageLog | None = None, *, api_key: str = "",
               keys=None, masker: Masker | None = None,
               middlewares: list[dict] | None = None,
               sticky: StickySessions | None = None) -> FastAPI:
    """把依赖装配成 FastAPI 应用;所有请求逻辑都委托给 RequestPipeline。"""
    app = FastAPI(docs_url=None, redoc_url=None)
    registry = keys if keys is not None else KeyRegistry([], api_key=api_key)
    pipeline = RequestPipeline(providers, router, usage_log, registry,
                               masker or Masker(), sticky or StickySessions(),
                               middlewares)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__})

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/stats")
    async def stats() -> JSONResponse:
        return JSONResponse(usage_log.summarize() if usage_log else {})

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        ok, _, _ = registry.check(presented_key(request))
        if not ok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        ids: list[str] = list(pipeline.router.model_map)
        for name, up in pipeline.upstreams.items():
            for m in await up.list_models():
                ids.append(f"{name}/{m}")
        return JSONResponse({"object": "list",
                             "data": [{"id": m, "object": "model", "owned_by": "aviary"}
                                      for m in ids]})

    @app.post("/v1/chat/completions")
    async def chat(body: dict, request: Request) -> Response:
        return await pipeline.handle("openai", body, request)

    @app.post("/v1/messages")
    async def messages(body: dict, request: Request) -> Response:
        return await pipeline.handle("anthropic", body, request)

    @app.post("/v1/responses")
    async def responses_ep(body: dict, request: Request) -> Response:
        return await pipeline.handle("responses", body, request)

    @app.post("/v1beta/models/{model_path:path}")
    async def gemini_ep(model_path: str, body: dict, request: Request) -> Response:
        model = model_path.split(":")[0]
        stream = model_path.endswith(":streamGenerateContent") \
            or request.query_params.get("alt") == "sse"
        body = dict(body)
        body["stream"] = stream
        return await pipeline.handle("gemini", body, request, requested=model)

    return app
