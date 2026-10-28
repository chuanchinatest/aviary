"""用量记录与统计。"""

import json

from aviary.usage import UsageLog, UsageRecord


def test_write_and_summarize(tmp_path):
    log = UsageLog(str(tmp_path / "usage.jsonl"))
    rec = UsageRecord(requested_model="a/m1", provider="a", upstream_model="m1",
                      protocol_in="openai", protocol_out="openai", stream=True,
                      status="ok", attempts=1, latency_ms=120,
                      prompt_tokens=100, completion_tokens=50, cost_usd=0.000125)
    log.write(rec)
    rec2 = UsageRecord(requested_model="group/daily", provider="b", status="error",
                       error="upstream error 500: mock 临时故障")
    log.write(rec2)
    rows = [json.loads(l) for l in open(log.path, encoding="utf-8")]
    assert rows[0]["prompt_tokens"] == 100 and rows[1]["status"] == "error"
    s = log.summarize()
    assert s["total_requests"] == 2 and s["ok"] == 1
    assert s["prompt_tokens"] == 100 and s["completion_tokens"] == 50
    assert abs(s["cost_usd"] - 0.000125) < 1e-9
    assert s["by_model"]["a/m1"]["requests"] == 1


def test_env_interpolation(tmp_path):
    import os
    from aviary.config import load_raw
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "providers:\n  - name: x\n    type: openai\n"
        "    base_url: http://x\n    api_key: ${SECRET_KEY_X}\n", encoding="utf-8")
    os.environ["SECRET_KEY_X"] = "sk-test-123"
    raw = load_raw(str(cfg))
    assert raw["providers"][0]["api_key"] == "sk-test-123"
