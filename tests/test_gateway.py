"""网关端到端:双协议、流式、故障切换、鉴权、模型列表、路由组、用量记录。"""

import json

from fastapi.testclient import TestClient

from aviary.config import providers_from, router_from, usage_log_from
from aviary.gateway import create_app


def _client(app):
    return TestClient(app)


def test_healthz(app):
    with _client(app) as c:
        assert c.get("/healthz").json()["ok"] is True


def test_models_lists_providers_and_alias(app):
    with _client(app) as c:
        ids = [m["id"] for m in c.get("/v1/models", headers={"Authorization": "Bearer gkey"}).json()["data"]]
    assert "fast" in ids and "mockopen/mock-chat" in ids


def test_auth_required(app):
    with _client(app) as c:
        assert c.post("/v1/chat/completions", json={"model": "x"}).status_code == 401
        # 密钥正确时正常放行并拿到上游回复
        r = c.post("/v1/messages", json={"model": "mockopen/mock-chat",
                                         "max_tokens": 64,
                                         "messages": [{"role": "user", "content": "hi"}]},
                   headers={"x-api-key": "gkey"})
        assert r.status_code == 200


def test_openai_client_to_openai_upstream(app, usage_path):
    with _client(app) as c:
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer gkey"},
                   json={"model": "mockopen/mock-chat", "messages": [
                       {"role": "user", "content": "你好"}]})
    assert r.status_code == 200
    data = r.json()
    assert data["choices"][0]["message"]["content"].startswith("来自 mock-chat")
    assert data["usage"]["prompt_tokens"] == 21
    rows = [json.loads(l) for l in open(usage_path, encoding="utf-8")]
    assert rows[-1]["status"] == "ok" and rows[-1]["provider"] == "mockopen"
    assert rows[-1]["cost_usd"] is not None


def test_openai_client_to_anthropic_upstream(app):
    with _client(app) as c:
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer gkey"},
                   json={"model": "mockanthropic/claude-mock", "messages": [
                       {"role": "system", "content": "S"},
                       {"role": "user", "content": "你好"}], "stream": False})
    assert r.status_code == 200
    data = r.json()
    assert "claude-mock" in data["choices"][0]["message"]["content"]
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["prompt_tokens"] == 17


def test_anthropic_client_to_openai_upstream(app):
    with _client(app) as c:
        r = c.post("/v1/messages",
                   headers={"x-api-key": "gkey", "anthropic-version": "2023-06-01"},
                   json={"model": "mockopen/mock-chat", "max_tokens": 128,
                         "messages": [{"role": "user", "content": "你好"}]})
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "message"
    assert "mock-chat" in data["content"][0]["text"]
    assert data["usage"]["input_tokens"] == 21


def test_streaming_openai_client_anthropic_upstream(app):
    """OpenAI 客户端 ← Anthropic 上游的流式翻译:拼回完整文本与 usage。"""
    with _client(app) as c:
        with c.stream("POST", "/v1/chat/completions",
                      headers={"Authorization": "Bearer gkey"},
                      json={"model": "mockanthropic/claude-mock", "stream": True,
                            "messages": [{"role": "user", "content": "你好"}]}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            text, usage, done = [], None, False
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    continue
                d = json.loads(payload)
                for ch in d.get("choices") or []:
                    if ch.get("delta", {}).get("content"):
                        text.append(ch["delta"]["content"])
                    if ch.get("finish_reason"):
                        assert ch["finish_reason"] == "stop"
                if d.get("usage"):
                    usage = d["usage"]
    assert "".join(text) == "来自 claude-mock 的回答:你好!"
    assert usage and usage["prompt_tokens"] == 17 and usage["completion_tokens"] == 11
    assert done


def test_streaming_anthropic_client_openai_upstream(app):
    """Anthropic 客户端 ← OpenAI 上游的流式翻译:事件序列合法。"""
    with _client(app) as c:
        with c.stream("POST", "/v1/messages",
                      headers={"x-api-key": "gkey"},
                      json={"model": "mockopen/mock-chat", "max_tokens": 128,
                            "stream": True,
                            "messages": [{"role": "user", "content": "你好"}]}) as r:
            assert r.status_code == 200
            events = []
            for line in r.iter_lines():
                if line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
    assert events[0] == "message_start" and events[-1] == "message_stop"
    assert "content_block_delta" in events and "message_delta" in events
    assert "ping" in events


def test_streaming_tool_call_translation(app):
    """工具调用跨协议流式:OpenAI 客户端 ← Anthropic 上游 tool_use 块。"""
    with _client(app) as c:
        with c.stream("POST", "/v1/chat/completions",
                      headers={"Authorization": "Bearer gkey"},
                      json={"model": "mockanthropic/claude-mock-tool", "stream": True,
                            "messages": [{"role": "user", "content": "天气"}]}) as r:
            args, names = [], []
            for line in r.iter_lines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                d = json.loads(line[5:])
                for ch in d.get("choices") or []:
                    for tc in ch.get("delta", {}).get("tool_calls") or []:
                        if tc.get("function", {}).get("name"):
                            names.append(tc["function"]["name"])
                        if tc.get("function", {}).get("arguments"):
                            args.append(tc["function"]["arguments"])
    assert names == ["get_weather"]
    assert json.loads("".join(args)) == {"city": "杭州"}


def test_failover_group_order(app, usage_path):
    """group/daily:第一个成员固定 500,自动落到第二个,agent 无感。"""
    with _client(app) as c:
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer gkey"},
                   json={"model": "group/daily", "messages": [
                       {"role": "user", "content": "你好"}]})
    assert r.status_code == 200
    assert "claude-mock" in r.json()["choices"][0]["message"]["content"]
    rows = [json.loads(l) for l in open(usage_path, encoding="utf-8")]
    assert rows[-1]["requested_model"] == "group/daily"
    assert rows[-1]["provider"] == "mockanthropic" and rows[-1]["attempts"] == 2


def test_model_map_alias(app):
    with _client(app) as c:
        r = c.post("/v1/chat/completions",
                   headers={"Authorization": "Bearer gkey"},
                   json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["model"] == "mock-chat"


def test_all_upstreams_fail_returns_502(usage_path):
    """上游全部不可达(连接被拒)→ 502,并记录失败。"""
    raw = {
        "gateway": {"host": "127.0.0.1", "port": 8787, "api_key": "",
                    "default_provider": "dead"},
        "log": {"file": usage_path},
        "providers": [{"name": "dead", "type": "openai",
                       "base_url": "http://127.0.0.1:1", "api_key": "k",
                       "models": ["m1"], "timeout": 2}],
        "groups": [], "model_map": {},
    }
    providers = providers_from(raw)
    dead_app = create_app(providers, router_from(raw, providers),
                          usage_log_from(raw), api_key="")
    with _client(dead_app) as c:
        r = c.post("/v1/chat/completions", json={"model": "dead/m1",
                                                 "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502
    rows = [json.loads(l) for l in open(usage_path, encoding="utf-8")]
    assert rows[-1]["status"] == "error"
