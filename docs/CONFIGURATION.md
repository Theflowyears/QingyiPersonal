# 配置参考

## 品牌和人物

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SITE_DISPLAY_NAME` | `AI Bot Console` | 网站控制台显示名称 |
| `SITE_TAGLINE` | `BOT OPERATIONS` | 控制台副标题 |
| `BOT_DISPLAY_NAME` | `QQ AI 助手` | 机器人显示名称 |
| `BOT_PERSONA_NAME` | 跟随显示名称 | 人物名称 |
| `BOT_PERSONA` | 通用友善助手 | 系统人物设定 |
| `BOT_TRIGGER_WORDS` | 显示名称 | 逗号分隔群聊触发词 |
| `BOT_NODE_LABEL` | `机器人节点` | 控制台节点说明 |

## 链路

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ONEBOT_WS` | `ws://127.0.0.1:3001` | OneBot WebSocket |
| `ONEBOT_ACCESS_TOKEN` | 空 | OneBot 鉴权；留空时必须保证该端口不可从公网抵达 |
| `CEYLAN_ONEBOT_CONFIG` | 自动发现 | NapCat OneBot 配置路径 |

### 模型通道（实际主链路）

文本、视觉、摘要、长文、备胎各自是一组 `<PREFIX>_BASE_URL / _API_KEY / _MODEL / _MAX_TOKENS / _TIMEOUT / _REASONING_EFFORT / _NAME`：

| 前缀 | 角色 | 读取时机 |
|---|---|---|
| `LLM_TEXT_` | 日常水群主链路 | 进程启动时 |
| `LLM_VISION_` | 带图请求 | 进程启动时 |
| `LLM_SUMMARY_` | 记忆压缩（建议与主链路不同供应商，避免抢配额） | 进程启动时 |
| `LLM_DEEP_` | 长文/认真答 | 每次调用重读 |
| `LLM_SOL_` | 长文备胎（`temperature` 不发送） | 每次调用重读 |
| `LLM_FB1_` / `LLM_FB2_` | 故障转移链，由 `LLM_FALLBACK_PREFIXES` 逗号列举 | 进程启动时 |
| `LLM_TEXT_FALLBACK_` | 延迟劣化时的自动降档目标 | 每次调用重读 |

调参：`ADAPT_SLOW_SECONDS`（判定慢的阈值，默认 2.0）、`ADAPT_SLOW_STREAK`（连续次数，默认 4）、`ADAPT_COOLDOWN_SECONDS`（降档冷却，默认 600）、`LLM_RETRY_TIMES`（默认 3）。`reasoning_effort` 取值 `low/medium/high/none/minimal`，留空表示完全不发送思考开关。

### OpenClaw 兼容路径（已非必需）

`OPENCLAW_URL`、`OPENCLAW_GATEWAY_TOKEN`、`CEYLAN_OPENCLAW_CONFIG` 只用于向后兼容：`OPENCLAW_URL` 与 token 同时存在时 `call_openclaw` 才走旧网关，否则一律走上面的 `LLM_*` 通道。缺 token 也不再报错，直连模型即可正常工作。

## 隐私与权限

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BLOCKED_GROUP_IDS` | 空 | 逗号分隔；永不上传具体 ID |
| `ADMIN_QQ_IDS` | 空 | 可触发任务模式的账号 |
| `CEYLAN_BOT_TOKEN` | 无 | 控制面共享随机令牌 |

## 控制台地址

推荐只设置 `BOT_CONTROL_BASE_URL=https://你的域名`。也可分别覆盖 `CEYLAN_HEARTBEAT_URL`、`CEYLAN_TELEMETRY_URL`、`CEYLAN_CONTROL_URL`、`CEYLAN_KEYS_URL` 和 `CEYLAN_FUN_URL`。

URL 必须是普通 URL，例如 `https://example.com/api/bot/status`，不能写成 Markdown 链接 `[https://...](https://...)`。

机器人与网站同机部署时，这组地址一个都不需要填：状态走 `data/bot_status.json`，命令走 `data/control-v4/inbox/` 文件桥，只有心跳与详细遥测仍按各自定时器 POST。

## 参数优先级

`data/control-v4/settings.json`（面板热改，最高）> `config.env` > 代码默认值。面板能改的键由 `ceylan_control_core` 的 `RANGES`/`FLOATS`/`BOOLS` 限定；`settings.json` 在机器人进程启动时载入，因此手工编辑该文件后需要 `systemctl restart` 才生效。

## 视觉

`USE_DIRECT_MIMO_VISION=1` 且 `MIMO_VISION_BASE_URL`、`MIMO_VISION_API_KEY` 均已配置时，带图请求直连 MiMo；否则视觉请求走 `LLM_VISION_*` 通道。`VISION_ENABLE_THINKING` 留空表示由模型自身默认决定，`0`/`1` 才强制开关。任何 Key 都只能放在机器人主机的受保护环境文件中。

## 上下文和资源

常用变量包括 `MAX_HISTORY`、`ACTIVE_GROUP_CONTEXT_LIMIT`、`PERIODIC_REVIEW_INTERVAL_SECONDS`、`GROUP_MEMORY_COMPACT_INTERVAL_SECONDS`、`GROUP_MEMORY_MIN_NEW_MESSAGES`、`CEYLAN_MODEL_CONCURRENCY` 和 `CEYLAN_MAX_PENDING`。数值越大不一定越好；先使用示例默认值并观察模型延迟与 Token 用量。
