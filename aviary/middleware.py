"""网关中间件:对每个请求做声明式改写。对标 New API 的 param_override / system_prompt。

配置示例:
  middleware:
    - type: system_prompt          # 注入/覆盖 system
      prompt: "你是企业助手。"
      mode: prepend                # prepend | replace
    - type: param_override         # 改/删请求字段;命中 condition 才生效
      condition: {model: "gpt-.*"} # 可选,正则匹配 requested_model
      set: {temperature: 0.2}
      remove: [top_p]

自定义异常或耗时都不会让请求失败(中间件抛错则跳过该条)。
"""

from __future__ import annotations

import re


def apply_middlewares(mws: list[dict], body: dict, requested_model: str) -> dict:
    for mw in mws or []:
        try:
            body = _apply(mw, dict(body), requested_model)
        except Exception:
            continue
    return body


def _apply(mw: dict, body: dict, requested_model: str) -> dict:
    cond = mw.get("condition") or {}
    for field, pattern in cond.items():
        value = str(_dig(body, field) or "")
        if not re.match(pattern, value):
            return body
    mtype = mw.get("type")
    if mtype == "param_override":
        for k, v in (mw.get("set") or {}).items():
            body[k] = v
        for k in mw.get("remove") or []:
            body.pop(k, None)
    elif mtype == "system_prompt":
        prompt = mw.get("prompt") or ""
        mode = mw.get("mode", "prepend")
        msgs = body.get("messages")
        if msgs is not None and prompt:
            if msgs and msgs[0].get("role") == "system":
                if mode == "replace":
                    msgs[0]["content"] = prompt
                else:
                    msgs[0]["content"] = prompt + "\n" + (msgs[0].get("content") or "")
            else:
                msgs.insert(0, {"role": "system", "content": prompt})
    return body


def _dig(body: dict, dotted: str):
    cur = body
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur
