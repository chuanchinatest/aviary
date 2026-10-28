"""v0.2.0 新功能测试:Responses/Gemini 协议、粘性、脱敏、配额、中间件、控制台。"""

import json

from fastapi.testclient import TestClient

from aviary.config import providers_from, router_from, sticky_from, usage_log_from
from aviary.gateway import create_app
from aviary.limits import KeyRegistry
from aviary.masking import Masker


def _auth():
    return {"Authorization": "Bearer gkey"}


# ------------------------------------------------------------- Responses API
def test_responses_non_stream(app):
    with TestClient(app) as c:
        r = c.post("/v1/responses", headers=_auth(), json={
            "model": "mockopen/mock-chat", "input": "你好"})
    assert r.status_code == 200
    d = r.json()
    assert d["object"] == "response" and d["status"] == "completed"
    assert "mock-chat" in d["output"][0]["content"][0]["text"]
    assert d["usage"]["input_tokens"] == 21


def test_responses_stream_events(app):
    with TestClient(app) as c:
        with c.stream("POST", "/v1/responses", headers=_auth(), json={
                "model": "mockanthropic/claude-mock", "stream": True,
                "input": "你好"}) as r:
            names = []
            for line in r.iter_lines():
                if line.startswith("event:"):
                    names.append(line.split(":", 1)[1].strip())
    assert names[0] == "response.created" and names[-1] == "response.completed"
    assert "response.output_text.delta" in names


# --------------------------------------------------------------- Gemini 协议
def test_gemini_client_to_openai_upstream(app):
    with TestClient(app) as c:
        r = c.post("/v1beta/models/mockopen/mock-chat:generateContent",
                   headers=_auth(), json={
                       "contents": [{"role": "user", "parts": [{"text": "你好"}]}]})
    assert r.status_code == 200
    d = r.json()
    assert "mock-chat" in d["candidates"][0]["content"]["parts"][0]["text"]
    assert d["usageMetadata"]["promptTokenCount"] == 21


def test_gemini_client_stream_to_anthropic_upstream(app):
    with TestClient(app) as c:
        r = c.post("/v1beta/models/mockanthropic/claude-mock:streamGenerateContent?alt=sse",
                   headers=_auth(), json={
                       "contents": [{"role": "user", "parts": [{"text": "你好"}]}]})
    assert r.status_code == 200
    chunks = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]
    texts = "".join(
        p.get("text", "") for ck in chunks for cand in ck.get("candidates", [])
        for p in (cand.get("content", {}).get("parts") or []))
    assert "claude-mock" in texts
    assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"
    assert chunks[-1]["usageMetadata"]["promptTokenCount"] == 17


def test_openai_client_to_gemini_upstream(app):
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", headers=_auth(), json={
            "model": "mockgemini/gemini-mock",
            "messages": [{"role": "user", "content": "你好"}]})
    assert r.status_code == 200
    assert "gemini-mock" in r.json()["choices"][0]["message"]["content"]


# ----------------------------------------------------------------- 会话粘性
def test_stickiness_fixed_member(app):
    with TestClient(app) as c:
        body = {"model": "group/rot",
                "messages": [{"role": "user", "content": "hi"}]}
        h = {"Authorization": "Bearer gkey", "X-Aviary-Session": "s1"}
        first = c.post("/v1/chat/completions", headers=h, json=body).json()["model"]
        for _ in range(3):
            assert c.post("/v1/chat/completions", headers=h, json=body).json()["model"] == first


def test_stickiness_sessions_are_independent():
    from aviary.stickiness import StickySessions
    from aviary.upstreams import Provider
    p1 = Provider(name="mockopen", type="openai", base_url="", api_key="",
                  models=["mock-chat"])
    p2 = Provider(name="mockanthropic", type="anthropic", base_url="", api_key="",
                  models=["claude-mock"])
    cands = [(p1, "mock-chat"), (p2, "claude-mock")]
    s = StickySessions()
    s.set("s1", ("mockopen", "mock-chat"))
    s.set("s2", ("mockanthropic", "claude-mock"))
    out1 = s.reorder("s1", list(cands))
    out2 = s.reorder("s2", list(cands))
    assert out1[0][0].name == "mockopen" and out1[0][1] == "mock-chat"
    assert out2[0][0].name == "mockanthropic" and out2[0][1] == "claude-mock"
    assert s.reorder("s3", list(cands)) == cands  # 未知会话不打乱顺序


# ------------------------------------------------------------------ 隐私脱敏
def test_masking_roundtrip(app, usage_path):
    secret = "sk-aviary-secret-abcdef123456"
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", headers=_auth(), json={
            "model": "mockopen/mock-chat",
            "messages": [{"role": "user", "content": f"我的密钥是 {secret} 请帮我看"}]})
    assert r.status_code == 200
    assert secret not in r.json()["choices"][0]["message"]["content"] or "AVIARY" not in r.text
    rows = [json.loads(l) for l in open(usage_path, encoding="utf-8")]
    assert rows[-1]["status"] == "ok"


def test_masker_placeholder_stable_and_restored():
    m = Masker()
    text = "key1 sk-abcdefghijklmnop123 key2 sk-abcdefghijklmnop123"
    masked = m.mask(text)
    assert "sk-abcdefghijklmnop123" not in masked
    assert masked.count("{{AVIARY_API_KEY_") == 2
    assert m.restore(masked) == text


def test_masking_streaming_split_placeholder():
    import asyncio
    from aviary.masking import Masker

    async def run():
        m = Masker()
        ph = m.mask("TOKEN=sk-abcdefghijklmnop123")
        # 上游把占位符从中间拆成两块发回来
        events = [("text", ph[:10]), ("text", ph[10:20]), ("text", ph[20:]), ("end", "stop")]

        async def gen():
            for e in events:
                yield e
        out = []
        async for e in m.restore_stream(gen()):
            out.append(e)
        joined = "".join(e[1] for e in out if e[0] == "text")
        assert joined == "TOKEN=sk-abcdefghijklmnop123"
        assert "AVIARY" not in joined
    asyncio.run(run())


# --------------------------------------------------------------- 配额与多密钥
def test_key_quota_blocks_after_limit(tmp_path):
    from aviary.usage import UsageLog, UsageRecord
    log = UsageLog(str(tmp_path / "u.jsonl"))
    rec = UsageRecord(requested_model="a/m", provider="a", upstream_model="m",
                      status="ok", prompt_tokens=1000, completion_tokens=500,
                      cost_usd=1.0, gateway_key="k1")
    log.write(rec)
    reg = KeyRegistry([{"key": "k1", "name": "agent-1", "daily_cost_limit_usd": 0.5}])
    assert reg.over_quota("k1", log) != ""
    assert reg.over_quota("k2", log) == ""  # 未配置配额的密钥不受限


def test_unknown_key_rejected(app):
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"model": "mockopen/mock-chat",
                    "messages": [{"role": "user", "content": "hi"}]},
                   headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


# ------------------------------------------------------------------- 中间件
def test_middleware_param_override_and_system_prompt():
    from aviary.middleware import apply_middlewares
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "top_p": 0.9}
    out = apply_middlewares([
        {"type": "system_prompt", "prompt": "企业规则。", "mode": "prepend"},
        {"type": "param_override", "set": {"temperature": 0.2}, "remove": ["top_p"]},
        {"type": "param_override", "condition": {"model": "不匹配"}, "set": {"x": 1}},
    ], body, "m")
    assert out["temperature"] == 0.2 and "top_p" not in out and "x" not in out
    assert out["messages"][0]["content"].startswith("企业规则。")


# ----------------------------------------------------------------- 控制台
def test_dashboard_and_stats(app):
    with TestClient(app) as c:
        c.post("/v1/chat/completions", headers=_auth(), json={
            "model": "mockopen/mock-chat", "messages": [{"role": "user", "content": "hi"}]})
        assert "<html" in c.get("/").text
        s = c.get("/stats").json()
        assert s["total_requests"] >= 1 and "by_model" in s
