"""aviary 命令行:serve / models / stats / test。

  aviary serve -c config.yaml [--host 0.0.0.0 --port 8787]
  aviary models -c config.yaml
  aviary stats  -c config.yaml
  aviary test   -c config.yaml provider/model "你好"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import (gateway_from, load_raw, providers_from, router_from,
                     usage_log_from)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aviary", description="类 magpie 的本地 LLM 网关")
    ap.add_argument("command", choices=["serve", "models", "stats", "test"])
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("extra", nargs="*")
    args = ap.parse_args(argv)

    raw = load_raw(args.config)
    providers = providers_from(raw)
    router = router_from(raw, providers)

    if args.command == "serve":
        import uvicorn
        from .gateway import create_app
        from .usage import UsageLog
        g = gateway_from(raw)
        app = create_app(providers, router,
                         usage_log_from(raw) or UsageLog("data/usage.jsonl"),
                         api_key=g["api_key"])
        host = args.host or g["host"]
        port = args.port or g["port"]
        print(f"aviary 网关已启动 http://{host}:{port}  (OpenAI: /v1/chat/completions,"
              f" Anthropic: /v1/messages)")
        uvicorn.run(app, host=host, port=port, log_level="info")
        return 0

    if args.command == "models":
        from .upstreams import Upstream

        async def show():
            for p in providers:
                up = Upstream(p)
                ms = await up.list_models()
                print(f"{p.name} ({p.type}): {', '.join(ms) if ms else '(未配置/拉取失败)'}")
                await up.aclose()
        asyncio.run(show())
        return 0

    if args.command == "stats":
        from .usage import UsageLog
        log = usage_log_from(raw)
        s = (log or UsageLog("data/usage.jsonl")).summarize()
        print(f"请求 {s['total_requests']} 次, 成功 {s['ok']}; "
              f"输入 {s['prompt_tokens']} tok, 输出 {s['completion_tokens']} tok, "
              f"成本 ${s['cost_usd']:.6f}")
        for m, v in s["by_model"].items():
            print(f"  {m}: {v['requests']} 次, {v['prompt_tokens']}+{v['completion_tokens']} tok,"
                  f" ${v['cost_usd']:.6f}")
        return 0

    if args.command == "test":
        if len(args.extra) < 2:
            print("用法: aviary test -c config.yaml provider/model \"提示词\"", file=sys.stderr)
            return 2
        target, prompt = args.extra[0], args.extra[1]
        from .events import URequest
        from .upstreams import Upstream
        res = router.resolve(target)
        p, model = res.candidates[0]
        up = Upstream(p)

        async def go():
            meta = await up.complete(URequest(model=model, messages=[
                {"role": "user", "content": prompt}]))
            print(meta.get("text") or "(空回复)")
        try:
            asyncio.run(go())
        finally:
            asyncio.run(up.aclose())
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
