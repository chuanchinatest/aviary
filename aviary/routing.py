"""路由:模型寻址(provider/model、group/name、裸 model 别名)与路由组。

组模式(magpie 的核心能力,这里实现三种):
  order  — 按顺序用第一个,出错/不可用才落到下一个(故障切换)
  rotate — 每次请求轮换到下一个成员
  usage  — 优先用累计用量最少的成员
"""

from __future__ import annotations

import itertools
import time

from .upstreams import Provider, Upstream, UpstreamError


class Resolution:
    """一次解析的结果:按尝试顺序排列的 (provider, upstream_model)。"""

    def __init__(self, candidates: list[tuple[Provider, str]]):
        self.candidates = candidates


class Router:
    def __init__(self, providers: list[Provider], groups: list[dict],
                 model_map: dict[str, str] | None = None,
                 default_provider: str | None = None):
        self.providers = {p.name: p for p in providers}
        self.groups = {g["name"]: g for g in groups or []}
        self.model_map = model_map or {}
        self.default_provider = default_provider or (providers[0].name if providers else "")
        self._rotate = {name: itertools.cycle(list(g.get("members") or []))
                        for name, g in self.groups.items()}
        self._usage: dict[str, int] = {}

    # ------------------------------------------------------------------ naming
    def resolve(self, requested: str) -> Resolution:
        requested = requested.strip()
        if requested.startswith("group/"):
            gname = requested[6:]
            if gname not in self.groups:
                raise KeyError(f"未知路由组: {requested}")
            members = list(self.groups[gname].get("members") or [])
            mode = self.groups[gname].get("mode", "order")
            return Resolution(self._members_in_order(gname, members, mode))

        target = self.model_map.get(requested, requested)
        if "/" in target:
            pname, model = target.split("/", 1)
            if pname == "group":  # group/name 嵌套在别名里
                return self.resolve(target)
            if pname not in self.providers:
                raise KeyError(f"未知 provider: {pname}")
            return Resolution([(self.providers[pname], model)])
        # 裸模型名:在所有 provider 中找第一个声明了它的
        for p in self.providers.values():
            if p.models and target in p.models:
                return Resolution([(p, target)])
        if self.default_provider in self.providers:
            return Resolution([(self.providers[self.default_provider], target)])
        raise KeyError(f"无法解析模型: {requested}")

    def _members_in_order(self, gname: str, members: list[str], mode: str) -> list[tuple[Provider, str]]:
        order = list(members)
        if mode == "rotate" and self._rotate.get(gname):
            try:
                first = next(self._rotate[gname])
                if first in order:
                    i = order.index(first)
                    order = order[i:] + order[:i]
            except StopIteration:
                pass
        elif mode == "usage":
            order.sort(key=lambda m: self._usage.get(m, 0))
        out: list[tuple[Provider, str]] = []
        for m in order:
            if m.startswith("group/"):  # 组嵌套:展开
                out.extend(self.resolve(m).candidates)
            elif "/" in m:
                pname, model = m.split("/", 1)
                if pname in self.providers:
                    out.append((self.providers[pname], model))
        return out

    # ----------------------------------------------------------------- accounting
    def record_usage(self, requested: str, member: str) -> None:
        self._usage[member] = self._usage.get(member, 0) + 1
