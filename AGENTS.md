# AGENTS.md — aviary 贡献与 AI 协作指南

本文件供人类贡献者与 AI 编码代理共同遵守:改 aviary 代码前先读这里。

## 项目是什么

aviary 是一个**本地 LLM 网关**(类 yetone/magpie):所有 agent 指向它一个地址,
它把四种客户端协议(OpenAI Chat / Anthropic / OpenAI Responses / Gemini)的请求
翻译并路由到任意上游(openai 兼容 / anthropic / gemini),并在上游故障时自动切换。
Python ≥ 3.10,依赖仅 fastapi + uvicorn + httpx + pyyaml,无其他运行时依赖。

## 目录导览(改哪里)

```
aviary/
  protocols.py    # 客户端协议注册表:to_unified / from_meta / to_sse 三方向
  upstreams.py    # 上游适配注册表 ADAPTERS:complete / stream / models_path
  pipeline.py     # 请求管线 RequestPipeline:鉴权→配额→中间件→路由→粘性→脱敏→故障切换
  gateway.py      # 纯装配:只注册 FastAPI 端点,不写业务逻辑
  translate.py    # OpenAI ↔ Anthropic 请求/响应双向翻译(非流式)
  streaming.py    # OpenAI ↔ Anthropic SSE 翻译(内部统一事件模型)
  gemini.py / gemini_stream.py / responses_api.py   # 其余协议
  events.py       # 统一事件模型(四元组)+ EventTap
  routing.py / stickiness.py / masking.py / middleware.py / limits.py / usage.py
  dashboard.py    # Web 控制台页面
  config.py / cli.py
tests/            # pytest:39 个用例,含 mock 上游端到端
mock_upstream.py  # 三协议 mock(OpenAI :8850 / Anthropic :8851 / Gemini :8852)
```

## 硬性规则

1. **行为不变的重构,测试零改动**。`python -m pytest tests -q` 必须全绿(当前 39 个)。
   不许通过改测试让代码过;觉得测试错了,先在 PR 里说明。
2. **加客户端协议**:实现 `xxx_request_to_unified` / `unified_to_xxx_response` /
   `universe_to_xxx_sse` 三个函数,在 `protocols.py` 的 `PROTOCOLS` 注册一行,
   在 `gateway.py` 加一个端点。**不要**在 pipeline/gateway 里写 `if protocol == ...`。
3. **加上游类型**:在 `upstreams.py` 的 `ADAPTERS` 注册 complete/stream/models_path。
   禁止在 `Upstream` 方法里加 per-type 分支。
4. **统一事件模型是中枢**:上游适配器输出 `(text|tool|usage|end)` 四元组事件;
   客户端协议翻译器消费同样的四元组。新协议先归一到这一层,不做点对点翻译。
5. **配置敏感项**(api_key、price)走 `${VAR}` 环境变量插值;禁止把真实密钥写进
   代码、测试、`config.example.yaml`。
6. **运行时零新依赖**:需要新库时先论证;CI 会用 `pip install -e .` 干净环境验证。
7. 中文注释;公共函数写一行 docstring 说清输入输出。

## 命令

```bash
pip install -e ".[dev]"     # 安装(含 pytest)
python -m pytest tests -q   # 全量测试,39 个,~3.5s
ruff check aviary/          # lint,规则见 pyproject.toml(E4/E7/E9/F)
python mock_upstream.py     # 起 mock 上游做手工联调
```

## 网络测试约定

- 端到端测试一律连 `127.0.0.1` 的 mock 上游(`tests/conftest.py` 的 `_Servers`),
  端口用 `free_port()` 动态分配,不写死。
- 不要在单元测试里访问公网;公网地址只出现在 README/demo。

## 版本与发布

- 版本三处同步:`pyproject.toml`、`aviary/__init__.py`、README 徽标行。
- 改动写入 `CHANGELOG.md`(Keep a Changelog 风格)。
- 发布:打 tag `vX.Y.Z` 并推送到 GitHub。

## 提交约定

- 提交信息用 `type: 摘要`,type ∈ {feat, fix, refactor, docs, test, ci}。
- 纯重构必须标注"行为不变";修 bug 时说明发现途径(如 CI、冒烟)。
- PR 描述写清:目的 / 变更清单 / 验证方式(测试数、冒烟项)。
