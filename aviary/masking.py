"""隐私脱敏:请求出境前把密钥/令牌等换成稳定占位符,回复(含流式)中还原。

占位符形如 {{AVIARY_API_KEY_ab12cd}},同一原值每次得到同一占位符,
保证多轮对话、prompt cache 与思维链签名不被破坏。原值只存本进程内存。
"""

from __future__ import annotations

import hashlib
import re

DEFAULT_PATTERNS: list[tuple[str, str]] = [
    ("API_KEY", r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    ("API_KEY", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ("AWS_KEY", r"\bAKIA[0-9A-Z]{16}\b"),
    ("TOKEN", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("PRIVATE_KEY", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s]+-----END [A-Z ]*PRIVATE KEY-----"),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    ("EMAIL", r"\b[A-Za-z0-9._%+-]+@(?!example\.com|test\.|invalid\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
]


class Masker:
    def __init__(self, extra_patterns: list[str] | None = None,
                 never_mask: list[str] | None = None):
        self.rules = list(DEFAULT_PATTERNS)
        for i, pat in enumerate(extra_patterns or []):
            try:
                self.rules.append((f"CUSTOM_{i}", re.compile(pat)))
            except re.error:
                continue
        self.never = list(never_mask or [])
        self._values: dict[str, str] = {}      # placeholder -> original(仅内存)

    # ------------------------------------------------------------------ mask
    def mask(self, text: str) -> str:
        if not text:
            return text
        for kind, pat in self.rules:
            def repl(m, kind=kind):
                original = m.group(0)
                if any(n in original for n in self.never):
                    return original
                return self._placeholder(kind, original)
            text = re.sub(pat, repl, text)
        return text

    def _placeholder(self, kind: str, original: str) -> str:
        h = hashlib.sha256(original.encode()).hexdigest()[:6]
        ph = "{{AVIARY_" + kind + "_" + h + "}}"
        self._values.setdefault(ph, original)
        return ph

    # -------------------------------------------------------------- restore
    def restore(self, text: str) -> str:
        if not text or "AVIARY_" not in text:
            return text
        for ph, original in self._values.items():
            if ph in text:
                text = text.replace(ph, original)
        return text

    # ------------------------------------------- streaming-safe 还原迭代器
    async def restore_stream(self, events):
        """包装统一事件流:text 做跨块安全还原;tool 参数攒到流末统一还原。

        (工具参数里若含占位符,分块可能把占位符拆开,故延迟到 end 一次性还原。)
        """
        tail = ""
        held_args: dict[int, str] = {}
        held_meta: dict[int, tuple] = {}
        async for ev in events:
            if ev[0] == "text":
                full = tail + ev[1]
                restored = self.restore(full)
                cut = self._hold_cut(restored)
                yield ("text", restored[:cut])
                tail = restored[cut:]
            elif ev[0] == "tool":
                _, idx, id_, name, piece = ev
                held_args[idx] = held_args.get(idx, "") + (piece or "")
                held_meta[idx] = (id_, name)
            elif ev[0] == "end":
                for idx, args in held_args.items():
                    id_, name = held_meta.get(idx, (None, None))
                    yield ("tool", idx, id_, name, self.restore(args))
                if tail:
                    yield ("text", self.restore(tail))
                    tail = ""
                yield ev
            else:
                yield ev

    @staticmethod
    def _hold_cut(s: str) -> int:
        """若尾部有未闭合的 {{ ,从这里截断,防止占位符被拆成两半发出。"""
        i = s.rfind("{{")
        if i != -1 and "}}" not in s[i:]:
            return i
        return len(s)

    async def restore_tool_args(self, args: str) -> str:
        return self.restore(args)
