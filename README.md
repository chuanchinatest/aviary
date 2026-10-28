# aviary · 每个模型,一个入口

类似 [yetone/magpie](https://github.com/yetone/magpie) 的**本地 LLM 网关**:所有
agent 都指向这一个地址,模型、密钥、路由、计费都在这里管。

magpie 的核心思路是:agent 各管各的配置与密钥,而 magpie 用一个本地网关
(默认 `127.0.0.1:3425`)统一承接,把请求翻译、路由到任意上游,配额耗尽或上游
报错时自动切换,agent 无感 [22]。aviary 用 Python 实现了同一套核心能力:

| 能力 | 说明 |
| --- | --- |
| **协议互转** | 客户端说 OpenAI Chat,上游是 Anthropic?网关翻译,反之亦然;流式(SSE)、工具调用、用量都翻译到位 |
| **四种客户端协议** | OpenAI Chat `/v1/chat/completions`、Anthropic `/v1/messages`、OpenAI Responses `/v1/responses`、Gemini `/v1beta/models/{model}:generateContent/:streamGenerateContent` |
| **三种上游类型** | `openai` 兼容端点、`anthropic`、`gemini`,任意组合互转 |
| **统一寻址** | `provider/model`、`group/name`、`model_map` 别名,一个字符串决定谁应答 |
| **路由组** | `order`(故障切换)/ `rotate`(轮换)/ `usage`(最少使用优先) |
| **故障切换** | 上游 5xx / 429 / 断连自动落下一个成员;流式请求在向客户端产出前都可切换 |
| **会话粘性** | 同一会话(请求头 `X-Aviary-Session` 或按对话内容哈希)固定到同一成员,保住上游 prompt cache;失败换人后记住新成员 |
| **隐私脱敏** | 请求里的 API 密钥/令牌(AWS、GitHub、OpenAI、Slack 等格式)出境前替换为稳定占位符,回复与流式分块中还原;支持自定义正则与豁免列表 |
| **网关中间件** | `param_override`(改/删请求字段,可按模型条件生效)、`system_prompt`(注入/前置系统提示) |
| **多密钥与配额** | 每个客户端密钥可有名称与每日 token/费用上限,超限返回 429;`/stats` 按密钥分列 |
| **网关鉴权** | 网关密钥,客户端 `Authorization: Bearer <key>` 或 `x-api-key`;不配置则开放 |
| **用量与成本** | 每次调用一行 JSONL(token、延迟、成本、重试次数、所用密钥),`aviary stats` 汇总 |
| **模型发现** | OpenAI 型自动拉 `/v1/models`;`/v1/models` 合并所有 provider + 别名 |
| **Web 控制台** | 浏览器打开网关根路径即仪表盘;`/stats` 返回 JSON |

MIT;运行依赖仅 fastapi + uvicorn + httpx。

## 快速开始

```bash
pip install -e .            # 或 pip install -r requirements.txt

# 1) 起三个 mock 上游(OpenAI :8850,Anthropic :8851,Gemini :8852),体会协议互转
python mock_upstream.py

# 2) 用 config.mock.json 起网关
aviary serve -c config.mock.json

# 3) OpenAI 客户端 → Anthropic 上游(网关自动翻译)
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"mockanthropic/claude-mock","messages":[{"role":"user","content":"你好"}]}'

# 4) Anthropic 客户端 → OpenAI 上游
curl http://127.0.0.1:8787/v1/messages \
  -H 'content-type: application/json' \
  -d '{"model":"mockopen/mock-chat","max_tokens":128,"messages":[{"role":"user","content":"你好"}]}'

# 5) 路由组:第一个成员固定失败,自动切换到第二个
curl http://127.0.0.1:8787/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"group/daily","messages":[{"role":"user","content":"你好"}]}'

# 6) Gemini 客户端 → OpenAI 上游(网关自动翻译)
curl 'http://127.0.0.1:8787/v1beta/models/mockopen/mock-chat:generateContent' \
  -H 'content-type: application/json' -H 'x-goog-api-key: <网关密钥>' \
  -d '{"contents":[{"role":"user","parts":[{"text":"你好"}]}]}'

# 7) OpenAI Responses 协议 → 任意上游
curl http://127.0.0.1:8787/v1/responses -H 'content-type: application/json' \
  -H 'Authorization: Bearer <网关密钥>' \
  -d '{"model":"mockanthropic/claude-mock","input":"你好"}'
```

接真实模型只需改配置(见 `config.example.yaml`),agent 侧一行环境变量:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1   OPENAI_API_KEY=<网关密钥>
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787   ANTHROPIC_API_KEY=<网关密钥>
```

## 配置

```yaml
gateway: {host: 127.0.0.1, port: 8787, api_key: "", default_provider: deepseek}
log: {file: data/usage.jsonl}
providers:
  - name: deepseek
    type: openai                    # openai 兼容端点(vLLM/Ollama/OpenRouter…)
    base_url: https://api.deepseek.com/v1
    api_key: ${DEEPSEEK_KEY}        # 支持环境变量插值
    price: {input: 0.27, output: 1.1}   # $/1M token,成本估算用
  - name: claude
    type: anthropic
    base_url: https://api.anthropic.com
    api_key: ${ANTHROPIC_KEY}
    models: [claude-sonnet-4-5]
    # auth: Authorization           # 中转站只认 Bearer 头时
groups:
  - name: daily-coding
    mode: order                     # order | rotate | usage
    members: [deepseek/deepseek-chat, claude/claude-sonnet-4-5]
model_map: {fast: deepseek/deepseek-chat}
```

## v0.2.0 新功能:配置与使用

### 多客户端密钥与每日配额

给每个 agent/同事发自己的钥匙,超限自动 429;不配 `keys` 则退回单个 `gateway.api_key`:

```yaml
gateway:
  keys:
    - {key: "ak-claude-code", name: claude-code, daily_token_limit: 5000000}
    - {key: "ak-cicd",        name: cicd,        daily_cost_limit_usd: 5.0}
```

`aviary stats` 与 `/stats` 会按密钥分列请求数、token 与费用。

### 会话粘性(默认开启)

同一会话固定到同一上游成员,保住供应商的 prompt cache;失败切换后记住新成员。
会话标识:请求头 `X-Aviary-Session: <id>`,缺省按 (模型 + 首条 user 消息) 哈希。
TTL 默认 30 分钟:

```yaml
stickiness: {ttl_seconds: 1800}
```

### 隐私脱敏(默认关闭)

请求里的 API 密钥/令牌(OpenAI `sk-`、GitHub `ghp_`、AWS `AKIA`、Slack `xox` 等)
出境前替换为稳定占位符,回复与流式分块中自动还原;支持自定义正则与豁免:

```yaml
masking:
  patterns: ["internal-[A-Za-z0-9]{8}"]   # 额外的自定义正则
  never_mask: ["sk-公开测试密钥"]          # 不脱敏的例外
```

### 网关中间件

进入路由前改写请求,可按 `condition.model` 正则条件生效:

```yaml
middleware:
  - type: param_override
    set: {temperature: 0.2}
    remove: [top_p]
  - type: param_override
    condition: {model: "gpt-.*"}     # 仅匹配 requested_model 时生效
    set: {max_tokens: 4096}
  - type: system_prompt
    mode: prepend                    # prepend | replace
    prompt: "你是公司内部助手,回答需遵守合规规范。"
```

### Web 控制台与管理端点

| 端点 | 说明 |
| --- | --- |
| `GET /` | 仪表盘(浏览器直接打开) |
| `GET /stats` | 用量统计 JSON(总量、按模型、按密钥、按天) |
| `GET /healthz` | 健康检查 |
| `GET /v1/models` | 合并所有 provider 的模型 + 别名 |

### 四种客户端协议接入

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1              # OpenAI Chat / Responses
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787              # Anthropic Messages
export GOOGLE_GENAI_BASE_URL=http://127.0.0.1:8787           # Gemini(客户端 SDK 指向网关)
# Gemini 也可直接 curl:
curl 'http://127.0.0.1:8787/v1beta/models/claude/claude-sonnet-4-5:generateContent' \
  -H 'x-goog-api-key: <网关密钥>' -H 'content-type: application/json' \
  -d '{"contents":[{"role":"user","parts":[{"text":"你好"}]}]}'
```

## CLI

```bash
aviary serve -c config.yaml [--host 0.0.0.0 --port 8787]  # 起网关
aviary models -c config.yaml                              # 各 provider 的模型
aviary stats  -c config.yaml                              # 用量与成本汇总
aviary test   -c config.yaml deepseek/deepseek-chat "你好" # 单发一次直连测试
python -m aviary ...                                      # 不安装也可用
```

## 架构

```
Claude Code ─ Anthropic ─┐                     ┌─ OpenAI 兼容 (vLLM/Ollama/OpenRouter…)
Codex CLI   ─ OpenAI   ──┤  aviary 网关 :8787  ├─ Anthropic 兼容
Gemini CLI  ─ Gemini  ──┤  翻译·路由·记账·     ├─ Gemini 兼容
任意 agent  ─ 任意协议 ──┘  粘性·脱敏·配额     └─ 你的任何 base_url
```

- `aviary/translate.py` — 请求/响应双向翻译(messages、system、tools、stop_reason、usage)
- `aviary/streaming.py` — SSE 双向翻译(内部统一事件模型,组合走同一层)
- `aviary/responses_api.py` / `aviary/gemini.py` / `aviary/gemini_stream.py` — Responses 与 Gemini 客户端协议
- `aviary/routing.py`  — 寻址与路由组(order / rotate / usage)
- `aviary/stickiness.py` — 会话粘性
- `aviary/masking.py`  — 隐私脱敏与流式还原
- `aviary/middleware.py` — 请求中间件(参数改写、系统提示注入)
- `aviary/limits.py`   — 多密钥与每日配额
- `aviary/gateway.py`  — FastAPI 端点、故障切换与 Web 控制台
- `aviary/usage.py`    — JSONL 用量日志与统计

### 与 magpie 的差距( roadmap )

magpie 还包含订阅账号接入(Claude/ChatGPT/Copilot 订阅)、Bun 插件运行时、
TUI/菜单栏、远程同步等 [22]。aviary v0.2.0 已覆盖协议互转、路由组、故障切换、
会话粘性、隐私脱敏、中间件、多密钥配额与 Web 控制台,后续可扩展:

- 订阅账号接入(用 Claude/ChatGPT 订阅而非 API key 计费)
- 插件系统(请求/响应钩子)
- TUI / 菜单栏应用
- 每密钥会话隔离与远程配置同步

## 测试

```bash
python -m pytest tests -q   # 39 个用例:翻译单测 + 网关端到端(含流式、故障切换、四种协议、脱敏、配额、粘性、中间件)
```
