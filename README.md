# aviary · 每个模型,一个入口

类似 [yetone/magpie](https://github.com/yetone/magpie) 的**本地 LLM 网关**:所有
agent 都指向这一个地址,模型、密钥、路由、计费都在这里管。

magpie 的核心思路是:agent 各管各的配置与密钥,而 magpie 用一个本地网关
(默认 `127.0.0.1:3425`)统一承接,把请求翻译、路由到任意上游,配额耗尽或上游
报错时自动切换,agent 无感 [22]。aviary 用 Python 实现了同一套核心能力:

| 能力 | 说明 |
| --- | --- |
| **协议互转** | 客户端说 OpenAI Chat,上游是 Anthropic?网关翻译,反之亦然;流式(SSE)、工具调用、用量都翻译到位 |
| **统一寻址** | `provider/model`、`group/name`、`model_map` 别名,一个字符串决定谁应答 |
| **路由组** | `order`(故障切换)/ `rotate`(轮换)/ `usage`(最少使用优先) |
| **故障切换** | 上游 5xx / 429 / 断连自动落下一个成员;流式请求在向客户端产出前都可切换 |
| **网关鉴权** | 一个网关密钥,客户端 `Authorization: Bearer <key>` 或 `x-api-key` |
| **用量与成本** | 每次调用一行 JSONL(token、延迟、成本、重试次数),`aviary stats` 汇总 |
| **模型发现** | OpenAI 型自动拉 `/v1/models`;`/v1/models` 合并所有 provider + 别名 |

MIT;运行依赖仅 fastapi + uvicorn + httpx。

## 快速开始

```bash
pip install -e .            # 或 pip install -r requirements.txt

# 1) 起两个 mock 上游(OpenAI :8850,Anthropic :8851),体会协议互转
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
任意 agent  ─ 任意协议 ──┘  翻译·路由·记账     └─ 你的任何 base_url
```

- `aviary/translate.py` — 请求/响应双向翻译(messages、system、tools、stop_reason、usage)
- `aviary/streaming.py` — SSE 双向翻译(内部统一事件模型,四种组合走同一层)
- `aviary/routing.py`  — 寻址与路由组(order / rotate / usage)
- `aviary/gateway.py`  — FastAPI 端点与故障切换
- `aviary/usage.py`    — JSONL 用量日志与统计

### 与 magpie 的差距( roadmap )

magpie 还包含订阅账号接入(Claude/ChatGPT/Copilot 订阅)、隐私脱敏、插件与网关
中间件、TUI/菜单栏、本地网络共享等 [22]。aviary 当前聚焦网关内核,后续可扩展:

- OpenAI Responses / Gemini 协议
- 会话粘性(同一对话固定到已应答账号,保住上游 prompt cache)
- 隐私脱敏(请求出境前掩码、回复中还原)
- 网关中间件(参数改写、敏感词、model-map 之外的路由策略)
- 每密钥/每日配额限制

## 测试

```bash
python -m pytest tests -q   # 25 个用例:翻译单测 + 网关端到端(含流式与故障切换)
```
