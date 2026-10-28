"""会话粘性:同一会话固定到同一上游成员,保住供应商的 prompt cache。

会话标识:请求头 X-Aviary-Session,缺省用 (model + 首条 user 消息) 的哈希。
成员失败被切换时更新映射,下次直接用新成员。
"""

from __future__ import annotations

import hashlib
import time


class StickySessions:
    def __init__(self, ttl_seconds: float = 1800.0):
        self.ttl = ttl_seconds
        self._map: dict[str, tuple[str, float]] = {}  # session -> (member, expires_at)

    @staticmethod
    def derive(header_value: str | None, model: str, messages: list) -> str:
        if header_value:
            return f"h:{header_value}"
        first_user = next(
            (m.get("content") for m in messages
             if isinstance(m, dict) and m.get("role") == "user"), "")
        if not isinstance(first_user, str):
            first_user = str(first_user)
        h = hashlib.sha256(f"{model}\x00{first_user}".encode()).hexdigest()[:16]
        return f"a:{h}"

    def get(self, session: str) -> str | None:
        item = self._map.get(session)
        if not item:
            return None
        member, expires = item
        if time.time() > expires:
            del self._map[session]
            return None
        return member

    def set(self, session: str, member: str) -> None:
        self._map[session] = (member, time.time() + self.ttl)

    def reorder(self, session: str, candidates: list[tuple]) -> list[tuple]:
        """把粘住的成员排到最前(若它在候选里);找不到或已失效则原序。"""
        member = self.get(session)
        if not member:
            return candidates
        keyed = {(p.name, model): i for i, (p, model) in enumerate(candidates)}
        idx = keyed.get(member)
        if idx is None:
            return candidates
        return [candidates[idx]] + candidates[:idx] + candidates[idx + 1:]
