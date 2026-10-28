"""多客户端密钥与配额:每个网关密钥可有名称与每日 token/费用上限,超限返回 429。

计数来源:usage.jsonl 里当天该 key 的成功请求。未配置 keys 列表时,
gateway.api_key 单密钥模式不受配额限制。
"""

from __future__ import annotations

import datetime as _dt

from .usage import UsageLog


class KeyRegistry:
    def __init__(self, keys: list[dict], api_key: str = ""):
        self.by_key: dict[str, dict] = {k["key"]: k for k in keys or []}
        self.legacy_key = api_key  # 旧式单密钥

    def check(self, presented: str) -> tuple[bool, str, str | None]:
        """返回 (是否放行, 失败原因, key 名称)。"""
        if not self.by_key and not self.legacy_key:
            return True, "", None  # 未配置任何密钥:开放模式
        if presented in self.by_key:
            return True, "", self.by_key[presented].get("name")
        if not self.by_key and self.legacy_key and presented == self.legacy_key:
            return True, "", None
        return False, "无效的网关密钥", None

    def quota_of(self, key: str) -> dict | None:
        return self.by_key.get(key)

    def over_quota(self, key: str, log: UsageLog, now: _dt.datetime | None = None) -> str:
        """超限返回原因,否则返回空串。"""
        cfg = self.quota_of(key)
        if not cfg:
            return ""
        day = (now or _dt.datetime.now()).strftime("%Y-%m-%d")
        s = log.summarize_key_day(key, day)
        tok_limit = cfg.get("daily_token_limit")
        cost_limit = cfg.get("daily_cost_limit_usd")
        if tok_limit and s["tokens"] >= tok_limit:
            return f"密钥 {cfg.get('name', key[:8])} 今日 token 已达上限 {tok_limit}"
        if cost_limit and s["cost_usd"] >= cost_limit:
            return f"密钥 {cfg.get('name', key[:8])} 今日费用已达上限 ${cost_limit}"
        return ""
