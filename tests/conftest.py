"""测试夹具:起 mock 上游(OpenAI + Anthropic),构建网关。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mock_upstream import make_anthropic_app, make_gemini_app, make_openai_app  # noqa: E402
from aviary.config import (providers_from, router_from, sticky_from,
                           usage_log_from, keys_from, masker_from)  # noqa: E402
from aviary.gateway import create_app  # noqa: E402


@pytest.fixture()
def raw_config(tmp_path):
    return {
        "gateway": {"host": "127.0.0.1", "port": 8787, "api_key": "gkey",
                    "default_provider": "mockopen"},
        "log": {"file": str(tmp_path / "usage.jsonl")},
        "providers": [
            {"name": "mockopen", "type": "openai", "base_url": "http://mockopen",
             "api_key": "k", "models": ["mock-chat", "mock-flaky", "mock-tool"],
             "price": {"input": 0.5, "output": 1.5}},
            {"name": "mockanthropic", "type": "anthropic",
             "base_url": "http://mockanthropic", "api_key": "k",
             "models": ["claude-mock", "claude-mock-tool"],
             "price": {"input": 3.0, "output": 15.0}},
            {"name": "mockgemini", "type": "gemini",
             "base_url": "http://mockgemini", "api_key": "k",
             "models": ["gemini-mock"], "price": {"input": 1.0, "output": 4.0}},
        ],
        "groups": [
            {"name": "daily", "mode": "order",
             "members": ["mockopen/mock-flaky", "mockanthropic/claude-mock"]},
            {"name": "rot", "mode": "rotate",
             "members": ["mockopen/mock-chat", "mockanthropic/claude-mock"]},
        ],
        "model_map": {"fast": "mockopen/mock-chat"},
    }


class _Servers:
    """把两个 mock 上游挂到随机空闲端口,避免与本机其他进程冲突。"""

    def __init__(self):
        import socket
        import threading
        import time

        def free_port() -> int:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            return port

        self.openai_port = free_port()
        self.anthropic_port = free_port()
        self.gemini_port = free_port()
        c1 = uvicorn.Server(uvicorn.Config(make_openai_app(), host="127.0.0.1",
                                           port=self.openai_port, log_level="error"))
        c2 = uvicorn.Server(uvicorn.Config(make_anthropic_app(), host="127.0.0.1",
                                           port=self.anthropic_port, log_level="error"))
        c3 = uvicorn.Server(uvicorn.Config(make_gemini_app(), host="127.0.0.1",
                                           port=self.gemini_port, log_level="error"))
        for c in (c1, c2, c3):
            threading.Thread(target=c.run, daemon=True).start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if c1.started and c2.started and c3.started:
                break
            time.sleep(0.1)


@pytest.fixture(scope="session")
def servers():
    return _Servers()


@pytest.fixture()
def app(raw_config, servers):
    raw_config["providers"][0]["base_url"] = \
        f"http://127.0.0.1:{servers.openai_port}/v1"
    raw_config["providers"][1]["base_url"] = \
        f"http://127.0.0.1:{servers.anthropic_port}"
    raw_config["providers"][2]["base_url"] = \
        f"http://127.0.0.1:{servers.gemini_port}"
    providers = providers_from(raw_config)
    router = router_from(raw_config, providers)
    log = usage_log_from(raw_config)
    return create_app(providers, router, log, keys=keys_from(raw_config),
                      masker=masker_from(raw_config), sticky=sticky_from(raw_config))


@pytest.fixture()
def usage_path(raw_config):
    return raw_config["log"]["file"]
