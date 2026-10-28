"""用量记录:每次网关调用落一行 JSONL;stats 汇总。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, fields


@dataclass
class UsageRecord:
    ts: float = 0.0
    requested_model: str = ""
    provider: str = ""
    upstream_model: str = ""
    protocol_in: str = ""   # openai | anthropic
    protocol_out: str = ""  # openai | anthropic
    stream: bool = False
    status: str = "ok"      # ok | error:<provider> | client_error
    attempts: int = 1
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    error: str = ""
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({k: getattr(self, k) for k in (
            f.name for f in fields(self)) if k != "extra"} | self.extra,
            ensure_ascii=False)


class UsageLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, rec: UsageRecord) -> None:
        if not rec.ts:
            rec.ts = time.time()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(rec.to_json() + "\n")

    # ------------------------------------------------------------------ stats
    def summarize(self) -> dict:
        rows = []
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        out: dict = {"total_requests": len(rows),
                     "ok": sum(1 for r in rows if r.get("status") == "ok"),
                     "prompt_tokens": 0, "completion_tokens": 0,
                     "cost_usd": 0.0, "by_model": {}}
        for r in rows:
            out["prompt_tokens"] += r.get("prompt_tokens", 0)
            out["completion_tokens"] += r.get("completion_tokens", 0)
            if r.get("cost_usd"):
                out["cost_usd"] += r["cost_usd"]
            key = r.get("requested_model") or "?"
            m = out["by_model"].setdefault(key, {"requests": 0, "prompt_tokens": 0,
                                                 "completion_tokens": 0, "cost_usd": 0.0})
            m["requests"] += 1
            m["prompt_tokens"] += r.get("prompt_tokens", 0)
            m["completion_tokens"] += r.get("completion_tokens", 0)
            if r.get("cost_usd"):
                m["cost_usd"] += r["cost_usd"]
        return out
