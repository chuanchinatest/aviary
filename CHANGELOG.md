# 更新日志

## v0.2.0 (2026-10-28)

### 新增
- **四种客户端协议**:OpenAI Chat `/v1/chat/completions`、Anthropic `/v1/messages`、
  OpenAI Responses `/v1/responses`、Gemini `/v1beta/models/{model}:generateContent/:streamGenerateContent`
- **Gemini 型上游**:`type: gemini` 的 provider,任意协议组合互转(含流式)
- **会话粘性**:`X-Aviary-Session` 或按对话内容哈希,同一会话固定上游成员,
  保住 prompt cache;失败换人后记住新成员
- **隐私脱敏**:sk-/ghp_/AKIA/xox 等密钥出境前掩码,回复与流式(含跨块拆断)中还原;
  支持自定义正则与豁免列表
- **网关中间件**:`param_override`(按模型条件改/删字段)、`system_prompt`(前置/替换)
- **多客户端密钥与配额**:`gateway.keys` 每密钥可命名并设每日 token/费用上限,超限 429
- **Web 控制台**:`GET /` 仪表盘、`GET /stats` JSON(按模型/密钥/天分列)
- mock 上游新增 Gemini(:8852),共三个 mock
- GitHub Actions CI(Python 3.10 / 3.12 矩阵)

### 修复
- Anthropic 型上游流式未强制 `stream: true`
- Gemini 请求解析丢失 `stream` 字段
- `pyyaml` 改为必装依赖(CI 发现 YAML 主路径缺依赖)

### 测试
- 25 → 39 个用例,全部通过

## v0.1.0 (2026-10-28)

- 首个版本:OpenAI ↔ Anthropic 协议互转、路由组(order/rotate/usage)、
  故障切换、网关鉴权、JSONL 用量与成本统计、`/v1/models` 聚合、CLI
