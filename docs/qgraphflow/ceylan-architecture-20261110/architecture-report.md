# 清漪当前架构、情感算法、门控与阶段路线报告

- 文档日期：2026-11-10；对象：当前工作树、已完成图模型及主控提供的港机只读运行快照。
- 交付范围：十二视图图册、源码证据模型、详细说明与脱敏运行快照；本轮会话修复另经测试、审查和快照部署，不修改频率配置。
- 图入口：[最终离线架构站点](final/index.html)；事实模型：[最终 graph.json](final/graph.json)；实测证据：[runtime-snapshot.json](runtime-snapshot.json)。
- 本文中的“她”沿用项目称呼；工程状态不构成意识、主观体验或心理测量有效性的证据。
- 架构约束：[AGENTS.md:23–35](../../../AGENTS.md#L23-L35)；阶段原则：[人格内核路线.md:337–354](../../人格内核路线.md#L337-L354)。

## 01. 证据等级、时间边界与结论摘要

### 01.1 四类证据必须分开

| 标记 | 含义 | 可以推出 | 不可以推出 |
|---|---|---|---|
| 源码事实 | 图节点 facts、source 与关系 site，以及本文定向阅读 | 存在的函数、公式、表结构、门控 | 云端已启用、所有路径均覆盖、真实效果良好 |
| 默认值 | 源码 fallback 或环境变量默认 | 未配置时采用的候选值 | 当前线上实值 |
| 运行快照 | 主控于 2026-11-10 港机只读实测并提供 | 该观测时点的字段与服务状态 | 长期稳定、未来状态、完整验收通过 |
| 拟议 | 路线退出条件与尚未接管的目标 | 下一阶段需要什么证据 | 已实现、已授权、已有生产收益 |

- 图模型统计：12 视图、115 节点、113 边、217 source refs、106/106 有锚关系；为主控已完成验证结果。
- 图语义、几何及 source 验证已通过；本文不重新执行绘图验证。
- BrowserConnector 未连接，浏览器视觉验收未完成；不得将几何检查写成视觉验收。
- CodeGraph 未安装；本文采用图模型锚与直接源码追踪，不声称做过 CodeGraph 分析。
- 最终图册已加入 2026-11-10 港机运行快照、线上频率实值和本轮会话修复边界；默认参数与线上覆盖值仍分开标注。
- 图锚对应图生成时工作树；本轮修复可使大桥后段行号少量移动，本文为修复点使用定向核对的新锚。
- `file:line` 链接均相对本深层目录：仓库根文件用 `../../../`，路线文档用 `../../`。

### 01.2 当前架构的一句话边界

- 当前是“QQ 事件编排 + 多库存储 + 多个自主 worker + 持久动作账本 + 情感旁路”，不是已完成的统一认知内核。
- 已参与表达的是实时好感 `affection`、上下文及旧 `_MOODS` 节奏；影子四维尚未驱动当前表达。
- `self_model` 提供稳定身份、事件、信念、版本、consent 与 outbox；API 存在不代表所有写入已归单一状态内核。
- 阶段 0/1 有实现基础，阶段 2 为影子运行；阶段 3–5 的统一接管仍为拟议。
- 依据：[AGENTS.md:25–35](../../../AGENTS.md#L25-L35)、[emotion_shadow.py:658–687](../../../bot/emotion_shadow.py#L658-L687)。

### 01.3 港机运行快照，不是本报告新探测

| 观测项 | 主控提供的 2026-11-10 实值 | 解释边界 |
|---|---|---|
| QQ 服务 | `qqbot active` | 当前进程状态，不等于端到端每条消息成功 |
| OneBot | NapCat `ws://127.0.0.1:3001` 已连接 | 连接成立，不是用户已读证据 |
| 影子版本 | `shadow-v2` | 与 v1 兼容实现区分 |
| 消费者 | `running=true`、`backlog=0` | 该时点心跳及积压状态 |
| 控制权 | `expression_control=false`、`stage3_ready=false` | 阶段 3 没有接管 |
| 观察评审 | `ready_for_review=true` | 只够进入影子观察评审 |
| 累计消费 | `processed=1778`、`cursor=3082` | 快照计数；不是完成率，也不是有效驱动总数 |
| 候选回放 | `candidate_integrated_replay` / `valence-integration-v1` | 独立反事实链，不是实时状态 |
| 候选统计 | `total=1416`、`included=24`、`excluded=1392` | included 指效价证据纳入，不是模型准确率 |
| 候选边界 | `counts_for_live=false`、`expression_control=false` | 不增加实时样本，不授权表达 |

## 02. 十二视图阅读指南

- 推荐顺序：01 总览 → 02 聊天 → 03 记忆 → 04 证据 → 09 门控 → 06/07/08 三条情感相关链 → 05 自主 → 12 多模态 → 11 桌面部署 → 10 路线。
- 实线调用和数据边表示源码关系；`optional` 是条件路径；拟议边不能读成生产调用。
- 模块框不等于独立进程；数据库节点不等于唯一数据源；一条聚合边不代表全部细节均有生产观测。

| 视图 | 读什么问题 | 首要入口锚 | 不应误读 |
|---|---|---|---|
| [01 系统总览](final/diagram-1-architecture.svg) | QQ、Bot、Web、模型、桌宠如何分界 | [ceylan_runtime.py:283–377](../../../bot/ceylan_runtime.py#L283-L377) | 不存在云端直控本地 MAA 的已实现边 |
| [02 聊天全链](final/diagram-2-architecture.svg) | 入站、装配、生成、后台查证与确认 | [qq_openclaw_bridge.py:7603](../../../bot/qq_openclaw_bridge.py#L7603) | 非每条消息都会调用模型或工具 |
| [03 上下文记忆](final/diagram-3-architecture.svg) | 多库存储与消费边界 | [memory_depth.py:247–275](../../../bot/memory_depth.py#L247-L275) | cognition.db 不是所有记忆的唯一真值 |
| [04 证据与 outbox](final/diagram-4-architecture.svg) | 谁、因何、向谁、执行结果是什么 | [self_model.py:32–171](../../../bot/self_model.py#L32-L171) | 表存在不等于真实证据覆盖率达标 |
| [05 自主行动](final/diagram-5-architecture.svg) | 求知、刷片、分享、好友私聊、签名 | [curiosity.py:365–408](../../../bot/curiosity.py#L365-L408) | 机会不是发送命令，多 worker 不是统一规划 |
| [06 实时好感](final/diagram-6-architecture.svg) | 当前关系分数怎样改变并影响表达 | [affection.py:769–910](../../../bot/affection.py#L769-L910) | 不是四维情感，也不是多维信任模型 |
| [07 情感影子](final/diagram-7-architecture.svg) | v1/v2 恢复、冲击、冻结与观察 | [emotion_dynamics.py:9–25](../../../bot/emotion_dynamics.py#L9-L25) | 没有影子到当前表达的控制边 |
| [08 效价候选](final/diagram-8-architecture.svg) | 标签如何核源并重建反事实轨迹 | [emotion_shadow.py:566–655](../../../bot/emotion_shadow.py#L566-L655) | 候选 done 不修改实时影子或好感 |
| [09 群发言门控](final/diagram-9-flowchart.svg) | 自动接话在哪些门被拒绝 | [qq_openclaw_bridge.py:4078–4302](../../../bot/qq_openclaw_bridge.py#L4078-L4302) | 图中 FORCE=8 是源码默认，不是线上 5 |
| [10 阶段路线](final/diagram-10-architecture.svg) | 0–5 与 2.5 如何按证据推进 | [phase_audit.py:563–707](../../../bot/phase_audit.py#L563-L707) | review_ready 不等于自动接管 |
| [11 部署与 MAA](final/diagram-11-architecture.svg) | 港机和本地授权执行面 | [desktop/runner.py:81–101](../../../desktop/runner.py#L81-L101) | 桌宠 P1 不等于云端 P2 已接通 |
| [12 多模态工具](final/diagram-12-architecture.svg) | 感知、产物、完成证据与外发 | [toolkit.py:232–290](../../../bot/toolkit.py#L232-L290) | 模型或供应商说完成，不等于产物已落盘或送达 |

## 03. 模块、进程与入口矩阵

| 模块/运行面 | 主入口或职责 | 状态/外部依赖 | 证据锚 |
|---|---|---|---|
| launcher | 读配置、单实例、启动 Bot/Web 子进程 | config.env、子进程 | [launcher.py:109–183](../../../launcher.py#L109-L183) |
| Bot runtime | WebSocket、回包分流、有界入站队列、worker 编排 | NapCat OneBot v11 | [ceylan_runtime.py:283–377](../../../bot/ceylan_runtime.py#L283-L377) |
| 聊天大桥 | 群/私聊、上下文、模型与后台工具、投递 | 会话锁、群 turn 锁、delivery 锁 | [qq_openclaw_bridge.py:416–486](../../../bot/qq_openclaw_bridge.py#L416-L486) |
| 会话仲裁 | 方向、可接话窗口、沉默、去复读 | 群新消息序号、上一轮回答 | [conversation_plan.py:133–208](../../../bot/conversation_plan.py#L133-L208) |
| 模型路由 | 文本、深度、视觉、视频等角色 | OpenAI-compatible；旧通道条件分支 | [llm_router.py:75–109](../../../bot/llm_router.py#L75-L109) |
| Managed Agent | 云端 REST 会话与轮询下载 | 第三方工具/skill 可用性 | [qwen_agent.py:113–219](../../../bot/qwen_agent.py#L113-L219) |
| 记忆装配 | 常驻身份、分层召回、预算 | persona、群事实、知识、摘要 | [memory_depth.py:34–81](../../../bot/memory_depth.py#L34-L81) |
| 人格成长 | 有源 journal 到候选合并与版本 | persona_store、巩固游标 | [persona_growth.py:578–661](../../../bot/persona_growth.py#L578-L661) |
| 证据自我 | 事件/人物/信念/动作/授权/撤证 | cognition.db | [self_model.py:32–171](../../../bot/self_model.py#L32-L171) |
| 实时好感 | 关键词、自述、承诺、面板写分 | relationship JSON | [affection.py:432–572](../../../bot/affection.py#L432-L572) |
| 情感影子 | 只读源事件、串行消费、独立状态 | shadow_run/shadow_event | [emotion_shadow.py:197–295](../../../bot/emotion_shadow.py#L197-L295) |
| 效价候选 | 语义态度标签与独立回放 | valence_candidates.db | [valence_candidates.py:123–188](../../../bot/valence_candidates.py#L123-L188) |
| Web 面板 | 只读状态、显式 consent/撤证/调参 | 文件/SQLite、部分 bearer 通道 | [ceylan_dashboard.py:1136–1229](../../../website/ceylan_dashboard.py#L1136-L1229) |
| 桌宠 Controller | 托盘、窗口、探测、模型线程与设备线程 | 本地 state、MAA/ADB | [qingyi_desktop.py:94–125](../../../desktop/qingyi_desktop.py#L94-L125) |

- Bot 内多个 worker 共享进程，不应画成各自独立服务。
- Web 与 Bot 的普通 Python 全局变量不共享；面板读写通过文件、SQLite 或明确通道传播。
- launcher 负责启动，不是 QQ 聊天请求的业务转发层。
- `qwen_agent` 的云端 Managed Agent 和本地 MaaCore 是两个无关执行系统。

## 04. 存储表与消费矩阵

### 04.1 cognition.db：证据、身份、版本与动作

| 表/对象 | 关键字段或语义 | 主要消费者/约束 | 锚 |
|---|---|---|---|
| person | 稳定 ID、QQ、aliases、first/last_seen | 事件和动作身份关联 | [self_model.py:60–66](../../../bot/self_model.py#L60-L66) |
| event | kind、ts、created、session、actor/topic、source、visibility、dedupe | 入站、工具结果、影子、证据回放 | [self_model.py:68–82](../../../bot/self_model.py#L68-L82) |
| episode | summary、event_ids、perspective、is_legacy | 有源经历与旧迁移区别 | [self_model.py:84–91](../../../bot/self_model.py#L84-L91) |
| belief | proposition、subject、confidence、status、source、legacy | 非 legacy 必须有真实证据 | [self_model.py:93–103](../../../bot/self_model.py#L93-L103) |
| belief_evidence | belief/event/relation/weight | support 与反证不能只存摘要 | [self_model.py:105–112](../../../bot/self_model.py#L105-L112) |
| belief_revision | old/new/reason/event_ids | 观点变化可追溯 | [self_model.py:114–122](../../../bot/self_model.py#L114-L122) |
| commitment | 对象、内容、到期、状态、event_ids | 未完成承诺不应因换模型丢失 | [self_model.py:124–133](../../../bot/self_model.py#L124-L133) |
| goal | owner、motive、origin、success、parent_event、终态 | 当前 curiosity 有具体建账入口 | [self_model.py:135–146](../../../bot/self_model.py#L135-L146) |
| action | goal、target、topic、visibility、scope、state、reason、event_ids、echo | 归属、授权、执行结果核验 | [self_model.py:148–164](../../../bot/self_model.py#L148-L164) |
| self_view_rev | text、reason、event_ids | 自我认识版本，不是无依据自评 | [self_model.py:166–171](../../../bot/self_model.py#L166-L171) |
| layer_revision | layer、hash、content、reason、ts | 生成请求绑定消费版本 | [self_model.py:33–37](../../../bot/self_model.py#L33-L37) |
| consent_record/revocation | recipient、action、decision、reason | deny 优先，撤销留历史 | [self_model.py:38–44](../../../bot/self_model.py#L38-L44) |
| action_outbox/outbox_part | logical_key、digest、payload、parts、state、receipt | I/O 前认领，不明结果不重认领 | [self_model.py:45–52](../../../bot/self_model.py#L45-L52) |
| evidence_retraction/invalidation | event、理由、对象关联 | 不删原事件；失效派生消费 | [self_model.py:53–59](../../../bot/self_model.py#L53-L59) |

- `ts` 是发生时间，`created` 是记录时间；不能把两者交换后宣称因果成立。
- event 中 actor/topic 字段经 API 可存稳定 person_id，不能只凭列名当作原始 QQ。
- 依据：[self_model.py:305–338](../../../bot/self_model.py#L305-L338)。

### 04.2 其它持久数据不应被 cognition.db 吞并描述

| 存储 | 内容/消费 | 已知边界 | 锚 |
|---|---|---|---|
| persona.db | 事实、经历、喜恶、元数据、tokens/importance/embedding | 默认检索不是 embedding；self.json 是兼容导出 | [persona_store.py:22–52](../../../bot/persona_store.py#L22-L52) |
| 群事实 JSON | 按群长期事实与倒排索引 | 按 group_id；不保证全部旧字段有事件级来源 | [group_memory_store.py:1004–1035](../../../bot/group_memory_store.py#L1004-L1035) |
| knowledge JSON | 世界知识、来源、verified/provenance、active | archive/quarantined 默认不注入 | [knowledge_base.py:97–123](../../../bot/knowledge_base.py#L97-L123) |
| history/journal/summary | 最近对话、私聊摘要、成长窗口 | 日志轮转不是完整生平账本 | [persona_growth.py:134–153](../../../bot/persona_growth.py#L134-L153) |
| member_memory | 按人称呼、关系资料 | 不能用群名片自动替代确定称呼 | [qq_openclaw_bridge.py:5911–5930](../../../bot/qq_openclaw_bridge.py#L5911-L5930) |
| affection.json | score/day/gain/loss/last/history/source | 实时关系信号，非影子情感 | [affection.py:769–824](../../../bot/affection.py#L769-L824) |
| affection_cooldown/nicks JSON | 原因冷却、昵称反查 | 目标绑定与权限 consent 不同 | [affection.py:22–37](../../../bot/affection.py#L22-L37) |
| emotion_shadow*.db | run/event/gap/retrospective | v1 冻结与 v2 分库选择，不覆盖旧轨迹 | [emotion_shadow.py:13–51](../../../bot/emotion_shadow.py#L13-L51) |
| valence_candidates.db | event 唯一标签、状态、置信度、模型、理由 | 候选不是事实或实时状态 | [valence_candidates.py:14–37](../../../bot/valence_candidates.py#L14-L37) |
| desktop/state | 本地授权、配额、别吵、跑单账本 | 与云端人格数据没有已实现的统一远控链 | [desktop/store.py:11–28](../../../desktop/store.py#L11-L28) |

## 05. 聊天主链与上下文装配

### 05.1 从入站到模型，不是无条件自动回复

1. runtime 先分流带 echo 的接口回包，等待器得到回执；普通入站才进入消息处理。
2. 检查消息类型、非自身、屏蔽群、message_id 去重和有界 pending 队列；默认上限 128。
3. 解析文本、图片、语音，记录原始事件；私聊原始消息先分别记账，再由会话路径组合。
4. 定向依据记录为 `addressed_to_self/address_basis`，引用、扮演也作为证据字段保留。
5. 群自动接话先通过机会、方向、归属、锁和新鲜度；直接 @、工具和私聊不应硬套同一概率路径。
6. `ask_openclaw` 进入会话串行区，检查 consent，冻结该轮 trigger event IDs。
7. 依据：[ceylan_runtime.py:346–377](../../../bot/ceylan_runtime.py#L346-L377)、[trigger_ledger.py:17–74](../../../bot/trigger_ledger.py#L17-L74)。

### 05.2 装配层次与职责

| 材料层 | 内容 | 装配边界/锚 |
|---|---|---|
| 稳定身份 | 常驻人格、身份摘要与边界 | [memory_depth.py:34–81](../../../bot/memory_depth.py#L34-L81) |
| 当前裁决 | 本轮是否说、是否办事、格式/拒绝规则 | [conversation_plan.py:331–337](../../../bot/conversation_plan.py#L331-L337) |
| 当前对象 | 收件人、称呼、性别等上下文与实时好感 | [affection.py:961–981](../../../bot/affection.py#L961-L981) |
| 当前事实 | 当前群长期事实、近期群窗口或私聊摘要 | [qq_openclaw_bridge.py:7777–7865](../../../bot/qq_openclaw_bridge.py#L7777-L7865) |
| 相关召回 | persona/群事实/知识去重与预算 | [recall_link.py:61–98](../../../bot/recall_link.py#L61-L98) |
| 感知材料 | 图像像素、表情含义卡、可见视觉笔记 | [vision_notes.py:343–362](../../../bot/vision_notes.py#L343-L362) |
| 当前节奏 | `_mood_hint` 旧链语气参考 | [qq_openclaw_bridge.py:2437–2463](../../../bot/qq_openclaw_bridge.py#L2437-L2463) |
| 请求审计 | 真正序列化请求 SHA256、各层版本与哈希 | [self_model.py:862–882](../../../bot/self_model.py#L862-L882) |

- identity 默认预算 420 字；缓存键包含 day、path 与 cognition token，不是永远等到次日才响应撤证。
- 承诺/目标和显著状态变化按路线要求应即时进入工作层；这条目标约束不等于当前所有路径已完成统一注入。
- 群事实动态预算默认基线 700 字；无锚词可收紧，至少三个锚词可放宽，不是固定把全部旧记忆塞给模型。
- 依据：[qq_openclaw_bridge.py:3360–3376](../../../bot/qq_openclaw_bridge.py#L3360-L3376)、[人格内核路线.md:300–302](../../人格内核路线.md#L300-L302)。

### 05.3 检索得分不是情绪算法

- `recall_link` 跨库 Jaccard 去重阈值为 0.55；重要度项权重 0.6，使用项 `0.75·ln(1+used)`。
- 时间项为 `3·2^(-age/30d)`；这是召回排序新近度，不是实时好感回归或影子状态半衰期。
- 知识注入有类别配额 8/6/6/5/5；active 门与 provenance 字段共同限制材料，不代表每条知识均已人工核验。
- 本轮 `build_recall_query` 去除 history 尾部与当前 text 完全相同的 user 轮，避免当前输入重复增权。
- 本地状态查询直接以当前句检索，不携带旧外部话题；普通指代问题仍可从最近三轮补实体。
- 依据：[recall_link.py:61–98](../../../bot/recall_link.py#L61-L98)、[qq_openclaw_bridge.py:3330–3357](../../../bot/qq_openclaw_bridge.py#L3330-L3357)。

### 05.4 生成后与确认后的界线

- 快回复和慢查询分支分开：先接住，后台查证；明确交代与可选补充使用不同过时规则。
- 首气泡 ≤2.5s 是验收目标，不是本轮实测成绩；本文没有新增线上延迟观测。
- 模型生成文本只是候选，必须经清理、复读/沉默、consent、outbox 与 OneBot 回执。
- 只有 accepted 后才写已交付 history、history_log 与 persona journal；不能把发送请求先记成对方已看到。
- journal 是成长原料，后续 LLM 巩固不能自证外部事实或动作完成。
- 依据：[qq_openclaw_bridge.py:517–559](../../../bot/qq_openclaw_bridge.py#L517-L559)、[persona_growth.py:578–661](../../../bot/persona_growth.py#L578-L661)。

## 06. 实时好感：当前实际使用的全部规则路径

### 06.1 信号、范围与存储

- `affection` 是按人的单一关系分数，不是四维情绪，不等于信任、亲密、依赖等维度已分别建模。
- 总范围 `[-50,100]`；普通人缺记录时默认 0；源码存在特定关系种子，本文不公开私人 QQ。
- `get()` 从 JSON 读值或种子；不调用 `_entry`，因此读分不触发跨天回归。
- `set_score` 是面板绝对值校正，仍裁剪总范围，记录 panel 来源和历史最近 12 条。
- 权威入口：[affection.py:39–69](../../../bot/affection.py#L39-L69)、[432–447](../../../bot/affection.py#L432-L447)、[769–797](../../../bot/affection.py#L769-L797)。

### 06.2 关键词兜底是按列表先后首命中，不是情感模型

| 顺序类别 | 命中示例 | 规则增量 |
|---|---|---|
| 负面 1 | 丑、难看、画得烂 | -4 |
| 负面 2 | 笨、蠢、废物、垃圾、没用 | -4 |
| 负面 3 | 滚、闭嘴、别烦我、讨厌你 | -5 |
| 负面 4 | 快点、赶紧、怎么这么慢 | -2 |
| 负面 5 | 别画了、别唱了、停下、算了 | -1 |
| 负面 6 | 没用、白痴、菜、弱 | -3；与前项重叠时前项优先 |
| 正面 1 | 谢谢、感谢你、辛苦了 | +2 |
| 正面 2 | 可爱、好看、真棒、厉害、牛、强 | +2 |
| 正面 3 | 喜欢、爱你、想你、抱抱、亲亲 | +3 |
| 正面 4 | 陪你、一起、咱们、我们 | +1 |
| 正面 5 | 记得、你上次、你之前说过 | +2 |
| 正面 6 | 别累着、休息、早点睡 | +3 |

- 先遍历负表，首命中立即返回；无负面才遍历正表；同句不累加多个类别。
- “最明显”是函数说明语义，实际执行是列表优先，不按绝对扣分幅度重新排序。
- 群入口先做他人归属门，不能把对别人的夸骂直接记给说话人对她的态度。
- 依据：[affection.py:52–69](../../../bot/affection.py#L52-L69)、[827–845](../../../bot/affection.py#L827-L845)、图 06 `aff-input`。

### 06.3 算法额度与原因冷却

- 原因冷却键为 `uid|reason`，默认 600 秒；不同原因不是同一个冷却键。
- `bump` 先裁剪单次增量到 ±5，再按当日正向 gain 与负向 loss 分别限额 40，最后裁剪总分。
- 正负额度分别计算，不是净变动 ±40；头部“每日 ±20”注释已经不能作为执行规则。
- 原因冷却先保存，再调用 bump；即便后续被总分或额度裁到零，也不能把返回原始 delta 当作精密实际变动。
- `judge_and_bump` 返回规则 delta 和新分描述，未严格返回 `new-old`；日志不能替代完整账本。
- 源码默认冷却不能当线上冷却实值；主控未提供该环境覆盖的独立实测值。
- 依据：[affection.py:800–824](../../../bot/affection.py#L800-L824)、[851–910](../../../bot/affection.py#L851-L910)。

### 06.4 跨天回归没有连续指数半衰期

- 只有进入 `_entry` 且 `day != today` 时才检查回归，同时清零当日 gain/loss。
- `gap=max(0,(now-last)/86400)`；若 `gap>3`，`score ← score·[1−min(0.4,0.1·(gap−3))]`。
- 正负分都向零缩放；这是惰性写路径回归，不能称每秒自然恢复或固定半衰期。
- `DECAY_PER_DAY=1.2` 常量未参与这个实际公式；不能拿该注释参数做线上推算。
- `_entry` 对内存对象的变化由后续保存路径落盘；面板读取不能自动宣称执行过该写路径。
- 依据：[affection.py:769–797](../../../bot/affection.py#L769-L797)。

### 06.5 本人自述、承诺、退款与目标绑定

- `apply_self` 可解析明确好感绝对值或增量，支持中文数字及负号；“涨到/降到 X”按目标值，不按加减 X。
- 自述绕过每日额度和原因冷却，但仍受总范围限制；不能概括所有写分路径为单次 ±5。
- 同 `(uid,完整文本)` 五秒去重；pending 好感询问默认 120 秒，解析成功才消费，允许分气泡铺垫。
- 未明确提好感的裸绝对值若一次跳变超过 30，会被误读安全网拒绝。
- `apply_promise` 处理短句“加/扣 X 分”；长度≤48、前导不能过长、需称呼/语气依据，单次裁剪 ±5，绕过日额度。
- 退款：无显式金额时需近 30 分钟扣分历史；显式金额分支可提供金额；单次最多 10，仍裁剪总分。
- 不将退款注释“不能超过扣前值”当已实现条件：实际该处上限使用 `MAX_SCORE-cur`，未按扣前值再限制。
- 目标优先顺序是明确 uid 尝试、点名反查、最近 pending；这是实际兼容规则，不是 consent 授权系统。
- `apply_self` 与 `apply_promise` 不能无脑顺序都试，否则共享去重表会阻断后者。
- 依据：[affection.py:164–235](../../../bot/affection.py#L164-L235)、[450–572](../../../bot/affection.py#L450-L572)、[589–705](../../../bot/affection.py#L589-L705)。

### 06.6 当前消费点与已知限制

- 档位：close≥55；warm≥20；normal>-10；cold>-30；其余 hostile；恰好 -10 为 cold，-30 为 hostile。
- `hint/query_line` 向提示词注入当前精确来源分数及报数规则；显示使用 round 后整数，不应声称浮点逐位展示。
- 陌生人也提供默认真值；旧记忆中已过期的好感数字不应覆盖本轮实时值。
- 提示词“必须报这个数”是约束，不是模型绝不会编数的形式证明。
- `proactive_dm` 读取分数筛选好友，门槛是 `>=15`；不因此绕过收件人、可见性或显式拒绝。
- 模型自述仍能写关系分，这是现有兼容机制；不应扩张成模型可直接写事件事实或所有人格状态。
- 依据：[affection.py:708–720](../../../bot/affection.py#L708-L720)、[930–981](../../../bot/affection.py#L930-L981)、[proactive_dm.py:170–209](../../../bot/proactive_dm.py#L170-L209)。

## 07. shadow-v1/v2：恢复、冲击、证据与版本

### 07.1 固定状态与公式

| 维度 | 基线 b | 时间常数 τ（秒） | 半衰期 τ·ln2 | 边界 |
|---|---:|---:|---:|---|
| valence 效价 | 0 | 7200 | 4990.66 秒，83.18 分 | [-1,1] |
| arousal 激活 | 0.25 | 1800 | 1247.66 秒，20.79 分 | [0,1] |
| control 掌控 | 0.5 | 3600 | 2495.33 秒，41.59 分 | [0,1] |
| attention 注意 | 0 | 600 | 415.89 秒，6.93 分 | [0,1] |

- 先解析恢复：`x(t+Δt)=b+(x(t)−b)·exp(−Δt/τ)`；有效目标时间不早于已有 state.at。
- 再施加冲击：`x_after=clip(x_recovered+δ,lower,upper)`；不是先裁剪增量再恢复。
- 半衰期描述相对基线偏离衰减一半，不是 state 数值本身减半。
- 六特质固定为 curiosity .6、sociability .5、caution .6、playfulness .6、sensitivity .5、expressiveness .5；不学习。
- 无单独习惯化衰减因子；重复冲击最终受 clip 饱和，不等于模型学会习惯刺激。
- 主控运行快照确认当前基线、τ 及下述 v2 步长与实现相符；它们仍是未校准计算参数。
- 依据：[emotion_core.py:22–38](../../../bot/emotion_core.py#L22-L38)、[93–105](../../../bot/emotion_core.py#L93-L105)、[emotion_dynamics.py:102–150](../../../bot/emotion_dynamics.py#L102-L150)。

### 07.2 冲击矩阵：顺序 v/a/c/attention

| 事件 | shadow-v1 | shadow-v2 | 前提 |
|---|---|---|---|
| 非空普通消息 | 0/0/0/+.02 | 0/0/0/+.02 | 被观察，不推断说话者心情 |
| 定向普通互动 | 同普通消息 | 0/+.02/0/+.02 | addressed_to_self 且非引用/扮演 |
| 直接夸奖/喜欢 | +.04/0/+.015/+.02 | +.04/+.02/+.015/+.02 | v1 三短语，v2 扩展正词 |
| 直接责备/纠正 | 无独立负效价规则 | -.025/+.02/-.015/+.02 | v2 负词命中且未先命中正词 |
| 工具明确成功 | 0/0/+.025/+.01 | 0/+.01/+.025/+.01 | payload.ok 为真正 bool True |
| 工具明确失败 | 0/0/-.025/+.01 | 0/+.01/-.025/+.01 | payload.ok 为真正 bool False |
| 工具 unknown | 零冲击 | 零冲击 | 不能把未知当失败或成功 |
| action_result 接口回执 | 零冲击 | 零冲击 | 技术接受不是社交回应/已读 |

- v1 夸奖短语为“谢谢你 / 你帮大忙 / 你好厉害”；普通消息只加注意，不增加激活。
- v2 复用 v1 初始评估，扩展明确指向她的正/负词；正词 `if` 先于负词 `elif`。
- v2 正词：喜欢你、最喜欢你、爱你、谢谢、辛苦了、乖、亲亲、抱抱、你好厉害、你帮大忙、帮我查清楚、宝宝真好、我真棒。
- v2 负词：你刚才说错了、你说错了、你理解错了、不是这个意思、别烦我、闭嘴、滚开、滚、讨厌你、你真笨、你真蠢。
- 这些是字符串包含规则，不是语义测量；短词可能产生语境偏差，不能因有词表就称识别准确。
- 依据：[emotion_core.py:191–233](../../../bot/emotion_core.py#L191-L233)、[emotion_dynamics.py:12–25](../../../bot/emotion_dynamics.py#L12-L25)、[113–137](../../../bot/emotion_dynamics.py#L113-L137)。

### 07.3 资格门与时间语义

- 持久消费者隔离 legacy/synthetic/verify/verification/test/probe 来源或会话前缀，以及 verification evidence_origin。
- 要求 payload 为有限 JSON 对象，时间有限且 `0<ts<=created`；坏输入记 quarantine，不能伪造缺失时间。
- 群/私聊 kind 归一为 message 后再评估；引用/转述/扮演保守阻断直接效价，普通注意负载可能仍存在。
- 消费按源 rowid；有效时间 `max(state.at,ts,created)`，不拿迟到原始 ts 让状态倒退。
- `created>now` 的未来事件延后消费，不抢先推进 cursor。
- quarantine 仍可计消费，但不等于真实有效驱动；processed、real_events、effective_events 必须分开读。
- 纯函数单次 apply_event 不自行全局去重；持久账本用 source_rowid 和 event_id 唯一约束实现消费边界。
- 依据：[emotion_shadow.py:91–126](../../../bot/emotion_shadow.py#L91-L126)、[238–279](../../../bot/emotion_shadow.py#L238-L279)、[emotion_core.py:271–278](../../../bot/emotion_core.py#L271-L278)。

### 07.4 冻结、恢复、版本与历史

- 参数用 canonical JSON SHA256 冻结；运行参数 hash 不符直接失败，不静默覆盖原观察。
- 版本标识 `shadow-v1`、`shadow-v2`；v2 词表标识 `VOCAB_VERSION=v2.1`。
- 词表常量不进入 PARAMETERS hash，故“参数 hash 相同”不能证明词表未变，必须同时核对版本与实现。
- `emotion_shadow.db` 若是 v1 冻结库，选择独立 `emotion_shadow_v2.db`；保留旧轨迹，不重写成 v2。
- 初次启动从当前事件末尾建立基线，旧历史走 retrospective；历史回放 `counts_for_live=false`。
- 事务 `BEGIN IMMEDIATE` 同时保护轨迹与游标；原 cursor 对应 event_id 不符则失败，不擅自重置。
- 心跳间隙超过 60 秒记 gap，不冒充连续观察；漏读事件仍可按 cursor 补读。
- v1 replay 按有效时间及稳定源序号排序；重复 ID 取排序首条；不同 tick 可核对解析恢复一致性。
- 依据：[emotion_shadow.py:41–51](../../../bot/emotion_shadow.py#L41-L51)、[197–295](../../../bot/emotion_shadow.py#L197-L295)、[emotion_core.py:291–385](../../../bot/emotion_core.py#L291-L385)。

### 07.5 观察评审条件，不是心理校准成功

- 源码默认要求有效心跳累计≥24 小时、有效驱动事件≥120、gap 比例≤.25；可由环境覆盖，不能自动当线上阈值实值。
- 轨迹非空且独立复算误差≤1e-10；聊天与工具均有驱动，四维均有真实非零覆盖。
- 源事件身份与重评需一致，积压为零且 cursor 身份一致；心跳距今小于 90 秒。
- 至少两种已确认工具，且含真实成功与失败；unknown 单列不充数。
- `ready_for_review` 为检查全通过；`stage3_ready=false`、`expression_control=false` 仍保持。
- 主控快照 ready=true 表示当时可评审，不证明规则心理有效，也不代替人工接管评审。
- 依据：[emotion_shadow.py:506–563](../../../bot/emotion_shadow.py#L506-L563)、[658–688](../../../bot/emotion_shadow.py#L658-L688)。

## 08. 独立效价候选整合回放

### 08.1 候选入口与标注约束

- 只给已观察且明确对她的消息建候选，方向依据包括 private、at、reply、direct_name。
- 事件原文本常保留最多 200 字，候选最多 300 字；核源对历史截断有受限前缀兼容。
- candidate 按 event_id 唯一，状态 pending/done/error，保存 model/confidence/reason/updated。
- 模型只判 praise/rebuke/neutral/uncertain，不直接填写四维数值。
- 默认 temperature=0、120 tokens、25 秒、无 fallback；每轮 drain 默认两条，最多三次，第三次失败转 error。
- 模型标签和 confidence 是候选解释，不是经校准的心理概率。
- 依据：[valence_candidates.py:14–37](../../../bot/valence_candidates.py#L14-L37)、[123–188](../../../bot/valence_candidates.py#L123-L188)。

### 08.2 源一致性门与单次整合

1. 每次请求只读重取 cognition 全历史与候选快照，不受实时 consumer cursor 限制，迟到标签可重建。
2. 检查真实源存在、来源/时间合法；未来事件、synthetic、引用、扮演和非定向证据不能充直接效价。
3. 候选须匹配 text、kind、session、ts、address_basis；旧 200 字截断只允许候选以原文本为前缀。
4. 有候选时必须 done、stance 为 praise/rebuke、有 model+reason、有限 confidence 在 [.8,1]。
5. pending/error/neutral/uncertain 抑制词表效价，不能不满意标签就悄悄回退正词。
6. 无候选时词表可作对照；先清零 v/c，再最多施加一次正或负效价/掌控冲击，不双计词表与标签。
7. a/attention 仍按基础消息评估；“效价 excluded”不等于该消息四维全部零。
8. 依据：[emotion_dynamics.py:29–90](../../../bot/emotion_dynamics.py#L29-L90)、[emotion_shadow.py:595–637](../../../bot/emotion_shadow.py#L595-L637)。

### 08.3 统计与状态隔离

- 实际标识是 `valence-integration-v1`，不是未经源码支持的 valence-candidate-v3。
- 输出 `mode=candidate_integrated_replay`，`counts_for_live=false`，`expression_control=false`。
- 每个请求局部建立 state/rows，恢复并裁剪到 now；不写 shadow_run/shadow_event，也不写 affection.json。
- 回放主要选消息及有标签源，不能宣称包含所有实时工具事件的完整四维轨迹。
- total/included/excluded 是源核验与效价纳入统计；不能解释为正确率、生产完成率或有效在线时长。
- 港机快照 1416/24/1392 属于该独立字段；不能与实时 processed1778/cursor3082 相除构造进度。
- 依据：[emotion_shadow.py:566–655](../../../bot/emotion_shadow.py#L566-L655)。

## 09. 所有关键门控：入口、机会、归属与频率

### 09.1 群自动接话的顺序

- 准入门：消息类型、非自身、屏蔽群、去重、有界队列；拒绝不调用生成来“补救”。
- 机会门：enabled、防抖、冷却、计数和概率；未到机会时等待。
- 占用门：group-turn 串行、发言 busy TTL 默认 45 秒、cursor 跳过已处理、绘图 pending 可阻断。
- 归属门：direction/joinable、候选目标在新窗口；明确别人双人对话不能被 FORCE 覆盖。
- 新鲜度门：生成前后检查新 seq；新消息可让候选失效，不把旧回答强塞进新话题。
- 表达门：沉默、控制标记、复读、重复、干跑；有内容也仍需下一层授权和投递。
- 依据：[ceylan_runtime.py:117–129](../../../bot/ceylan_runtime.py#L117-L129)、[qq_openclaw_bridge.py:4078–4302](../../../bot/qq_openclaw_bridge.py#L4078-L4302)。

### 09.2 默认值与本轮线上保持值分栏

| 参数 | 源码默认/配置默认 | 主控确认线上保持值 |
|---|---|---|
| GROUP_AUTO_REPLY_ENABLED | true | true |
| GROUP_AUTO_REPLY_DEBOUNCE | 2 秒 | 2 秒 |
| GROUP_AUTO_REPLY_COOLDOWN | 10 秒 | 6 秒 |
| GROUP_AUTO_REPLY_EVERY | 3 条 | 3 条 |
| GROUP_AUTO_REPLY_FORCE | 8 条机会 | 5 条机会 |
| GROUP_AUTO_REPLY_PROB | .55 | .9 |
| GROUP_AUTO_REPLY_MIN_HUMAN | 1 | 1 |
| reply_tokens | control DEFAULTS 80 | 150 |
| reply_chars | control DEFAULTS 30 | 45 |
| bubbles | control DEFAULTS 2 | 3 |
| delay_ms | control DEFAULTS 500 | 700 |

- 群源码另有主人机会默认每 2 条、概率 .85；不能据此替代线上独立字段的实测或推断全部主人路径。
- 本轮上述频率配置逐字节未变，主控用同解释器 AST 比对确认频率函数不变，线上 settings hash 不变。
- FORCE 是达到计数后的候选机会，不是绕过 joinable、consent 或锁的强制发言命令。
- 私聊既有骰子 1/2/3 句概率 50/30/20 保持；有图片至少两句、任务/亲密覆盖等既有规则也保持。
- bubble 上限、句数骰子、字符预算和群机会是不同层，不应拿某一层参数解释所有长度与频率。
- 依据：[qq_openclaw_bridge.py:3905–3914](../../../bot/qq_openclaw_bridge.py#L3905-L3914)、[ceylan_control_core.py:7–47](../../../bot/ceylan_control_core.py#L7-L47)；线上保持结论来自主控。

## 10. consent、数据可见性、工具验证与 outbox 门

### 10.1 同意不是好感推导

- 未撤回显式 deny 优先；作用域包括 send/dm/group，拒绝既在生成前检查，也在实际发送前再检查。
- 当前 unknown consent 不是全局 fail-closed；不能把“未明确同意”描述成所有路径自动拒绝。
- 好感≥15、主人关系、模型判断愿意、一次随机机会，均不能替代显式授权或撤销。
- 撤销 consent 记录保留历史，不等于删除发生过的外部行为。
- 依据：[self_model.py:894–924](../../../bot/self_model.py#L894-L924)、[qq_openclaw_bridge.py:407–434](../../../bot/qq_openclaw_bridge.py#L407-L434)。

### 10.2 可见性与撤证边界

- 行动应携带 topic/target/scope/evidence；“正在谈谁”和“发给谁”必须分开，不能按更高好感替换收件人。
- private 来源分享只能回来源人；私聊视觉笔记需要 session 完全匹配，敏感/private 不进入群。
- 当前 group 类型视觉笔记实现可跨群可见；不能把“按群隔离”当作已完成事实，这是明确边界/待评估缺口。
- 知识 archive/quarantined 默认不注入；普通群事实/旧摘要尚不能保证逐字段完整溯源。
- 撤证保留原 event；belief 可 invalidated 且 confidence=NULL，goal 无效化，其它对象追加失效关联。
- identity/personality 追加新消费版本；无字段级溯源的身份摘要保守整体抑制，不伪造精确局部重算。
- 缓存 token 支持即时失效；不能声称旧 JSON 摘要都已精确重算，更不能声称外部已发动作被撤回。
- 依据：[vision_notes.py:343–362](../../../bot/vision_notes.py#L343-L362)、[self_model.py:992–1066](../../../bot/self_model.py#L992-L1066)。

### 10.3 联网、网页与工具完成证据

- 本地好感/心情/身份查询先走局部语法，不因“查询”二字转成外部搜索；混合外部请求不能被整句误禁。
- 搜索结果须过完整实体、领域、回答相关与时效门；最多一条备选且保留实体，网页正文再次 verify。
- web_read 每跳检查 DNS/公网地址，受 GET、内容类型、字节与正文上限约束；不是任意内网访问工具。
- artifact 完成需本地非空文件；text 完成需非空文本；只有 Agent 叙述一般记 unknown。
- 工具 execution ID 去重；保存 session/visibility/evidence_origin；synthetic 验证证据不计真实影子样本。
- 生成产物完成、OneBot accepted、用户看见是三个独立命题；失败不得靠措辞变成成功。
- 依据：[web_search.py:755–807](../../../bot/web_search.py#L755-L807)、[web_read.py:168–186](../../../bot/web_read.py#L168-L186)、[toolkit.py:232–290](../../../bot/toolkit.py#L232-L290)。

### 10.4 投递状态机与幂等边界

- 概念动作态：proposed→authorized→generated→submitted→accepted/failed/unknown；当前发送可从 generated 建账，不保证逐态经过。
- outbox 先 prepare 固定 logical_key、payload 与 digest，再把 part 从 pending 原子 claim 到 submitted，最后进行 I/O。
- 明确 retcode=0/status=ok 才 accepted；明确拒绝 failed；超时/断连等无法证实结果保留 unknown。
- 分块仅全部 accepted 才整体 accepted；部分成功不能包装成整条已交付。
- submitted/unknown 不重新认领，避免进程崩溃后重复发送；这不是数学上的全链 exactly-once 保证。
- 终态产生 action_result 事件，不能无证据“复活”终态；技术回执不产生社会好感/已读证据。
- 文本和媒体共用 `_submit_chat` 关键边界；签名独立 social 路径不能一概声称具备全部聊天 outbox 门。
- 依据：[self_model.py:694–735](../../../bot/self_model.py#L694-L735)、[932–989](../../../bot/self_model.py#L932-L989)、[qq_openclaw_bridge.py:416–486](../../../bot/qq_openclaw_bridge.py#L416-L486)。

## 11. 自主 worker、预算与等待

| 路径 | 源码默认与选择逻辑 | 真正发送/完成门 | 锚 |
|---|---|---|---|
| curiosity | `Σw·2^(-age/14d)`，阈值3；满足10天、45天消退、失败3次止；默认行动0次/日、并发1 | 先 claim；有可追溯结果才 close_goal(done)，失败 abandoned/异常 unknown | [curiosity.py:283–408](../../../bot/curiosity.py#L283-L408) |
| 求知执行 | 来源种子保留 origin_events，同 tag 不反复加分 | goal+search action+tool_result；明确“未证实”也需真实查询依据 | 图05 `goal-execution`；[qq_openclaw_bridge.py:6597–6655](../../../bot/qq_openclaw_bridge.py#L6597-L6655) |
| 唯一分享队列 | 默认1条/日、间隔6h、同话题不重复；无实际内容不排队 | 冻结来源/收件人/内容，private 只回原人；accepted 才计；unknown 隔离 | [curiosity.py:477–553](../../../bot/curiosity.py#L477-L553) |
| bili_auto | 默认5条/日、间隔2h、本地时区9–23；拒新闻/已看/时长不符 | 必须实际理解后入分享；只读，不点赞收藏 | [bili_auto.py:100–127](../../../bot/bili_auto.py#L100-L127) |
| proactive_dm | 默认 UTC+8 9–23、日期种子2–4机会、总间隔90min、每人3天；真好友且分≥15 | 优先近期群里说话者，兜底高分/久未聊；claim/release/decline/done 分离 | [proactive_dm.py:97–209](../../../bot/proactive_dm.py#L97-L209) |
| signature | 最小20h、10–22时窗、每轮概率 .4 | social.set_signature 独立调用，不是严格每天一次 | 图05 `signature`；[qq_openclaw_bridge.py:7039](../../../bot/qq_openclaw_bridge.py#L7039) |
| growth/摘要等 | 新窗口后台巩固，解析失败不推进游标 | 保留原始 event IDs 与修订理由，不把总结当独立事实 | [persona_growth.py:578–661](../../../bot/persona_growth.py#L578-L661) |

- 以上是源码默认，实际自主额度可由面板 settings→autonomy_limits 热值覆盖 env/fallback；未提供线上实参就不填线上数字。
- “没有合适候选，等待”是合法结果；decline 不是工具失败，unknown 也不是可直接重试的 failed。
- 好友私聊喊错人应放弃，不因为主人好感更高把别人的话题转给主人。
- 当前多个 worker 各有预算/冷却；尚未完成所有目标统一预算、统一状态写入和统一仲裁。
- 能力边界中 NapCat 主动加好友、QQ 空间发布为 False；接口映射或文案不代表具备这些能力。
- 后台门关应写清原因；沉默 worker 不能被当作健康的自主等待来掩盖故障。
- 依据：[AGENTS.md:59–60](../../../AGENTS.md#L59-L60)、[人格内核路线.md:333–341](../../人格内核路线.md#L333-L341)。

## 12. 多模态：感知证据、产物与外发

- 图片先通过 NapCat get_image 取得本地/base64/freshURL；QQ CDN 过期引用不能当永久图片。
- 图片读取有大小上限、echo 异步回包；HTTP 为兜底，不表示任何 URL 都能被可信读取。
- ASR 非空转写后才进入对话；无内容不能伪造“听见了什么”，text 工具证据独立记账。
- 截图/照片可带原图，表情包可只带校准含义卡；含义卡不能冒充看见不存在的像素细节。
- VISION 默认并发2；入站后台识别与回复预算分开，不据此推出线上吞吐或首气泡时延。
- B站元数据、字幕、评论、视频理解分层；只取 metadata 不能宣称看完整视频。
- 视频角色默认 qwen3.8-omni-flash，不回退纯文本角色；角色名是默认，不是线上供应商可用性实测。
- 绘图有条件回退 flare→qwen-image→Agent→直连，可配置 agent 优先；并非每次执行全部通道。
- 说话 DashScope→可选 Agent→直连；唱歌3.0→3.1-next；Suno 未接入。
- tarot/zhouyi/fortune/video/excel 的 toolkit 映射不证明第三方 skill 当前已可用。
- media_public 限定目录、签名与到期，只在外部模型需取 URL 时使用；不等于私人媒体永久公开。
- 工具成功后外发仍过 consent/outbox/receipt；本地文件非空不证明 QQ 已接受。
- 依据：[napcat_channel.py:130–183](../../../bot/napcat_channel.py#L130-L183)、[asr.py:169–188](../../../bot/asr.py#L169-L188)、[bili_vision.py:97–148](../../../bot/bili_vision.py#L97-L148)。
- 产物与公开边界：[image_gen.py:538–555](../../../bot/image_gen.py#L538-L555)、[qwen_agent.py:113–219](../../../bot/qwen_agent.py#L113-L219)、[media_public.py:87–140](../../../bot/media_public.py#L87-L140)。

## 13. 本地桌宠与 MAA，不混作港机能力

- 云端模型是 qqbot.service→launcher→Bot/Web，连接 NapCat；部署白名单只有 bot/website/launcher。
- config.env 与 data 永不随普通同步覆盖；本地副本不是生产真值，本文未读取或发布配置密钥。
- desktop 不在该同步白名单；P1 纯本地，云端 P2 联动仍拟议，没有已实现港机→本机 MAA 远控边。
- Controller/PetWindow/Tray 可隐藏；关面板不等于退出；设备探测默认后台每8秒，模型池2、设备池1。
- discovery 只读本机 MAA 配置，gui.new.json 优先；任务按 SAFE/CONFIRM/MANUAL 分类，daily_plan 排除 MANUAL。
- 开工线程重新 check_gates：资源授权、GUI占用、ADB、设备；旧探测缓存不能代替即时授权。
- 资源任务参数指纹必须匹配；spending 在开工前消费一次性授权，不因普通聊天好感省略。
- Runner 顺序为装载、连接请求与连接结果确认、排队、AsstStart；请求被接受不等于已经连上模拟器。
- 回调曾有错误时 AllTasksCompleted 不应改成全成功；卡住不擅自硬停；旧未结束跑单补 interrupted。
- 本地账本记录真实跑单结果；主动气泡默认6/日、最小1200秒，模型生成后展示前再检查。
- voice.say 是文字气泡，不是 TTS 证据；无模型/超时/空内容显式兜底，结果失败不能靠沉默藏掉。
- 用户本机已安装 MaaCore.dll/ADB/模拟器为外部依赖；本文未加载 DLL、探测 ADB 或执行游戏任务。
- 依据：[AGENTS.md:14–15](../../../AGENTS.md#L14-L15)、[qingyi_desktop.py:94–161](../../../desktop/qingyi_desktop.py#L94-L161)、[runner.py:81–101](../../../desktop/runner.py#L81-L101)。
- 执行与结果：[runner.py:150–205](../../../desktop/runner.py#L150-L205)、[275–285](../../../desktop/runner.py#L275-L285)、[desktop/voice.py:57–83](../../../desktop/voice.py#L57-L83)。

## 14. 本轮主控修复范围与未动边界

### 14.1 故障证据与修复面

- 主控已读真实日志：用户查询好感，模型先判“不查”，后续兜底仍搜“好感度宝宝”，并错误交货。
- 这证明故障不只在模型意图判断；规则兜底、后台入口和模型查标记必须共同收口。
- `tool_intent.local_state_query` 用整句局部语法，识别本地好感/亲密/心情/身份问句；不是出现“好感”就全局禁网。
- 混合外部请求仍应放行其外部资料需求，不能把“查好感，再查外部信息”整个当纯本地问题。
- bridge `_explicit_lookup/_should_web_search/_classify_intent/is_owed_request`、后台入口与模型查标记共用本地查询边界。
- 本地查询不欠一次外部交货；不得搜索失败后再补一条本来不该存在的失败汇报。
- 依据：[tool_intent.py:25–55](../../../bot/tool_intent.py#L25-L55)、[qq_openclaw_bridge.py:2007–2011](../../../bot/qq_openclaw_bridge.py#L2007-L2011)、[3379–3387](../../../bot/qq_openclaw_bridge.py#L3379-L3387)。
- 其它收口锚：[5874–5891](../../../bot/qq_openclaw_bridge.py#L5874-L5891)、[5943–5949](../../../bot/qq_openclaw_bridge.py#L5943-L5949)、[6076–6077](../../../bot/qq_openclaw_bridge.py#L6076-L6077)、[8547–8553](../../../bot/qq_openclaw_bridge.py#L8547-L8553)。

### 14.2 重复开头与召回污染

- `cp.trim_repeated_opener` 只删除与旧回答完整首句一致、归一后6–24字符且无数字的开头；必须仍有新尾句。
- 不是按相似前缀粗暴整条丢弃；新尾、数字及 `#/+` 事实不能被这种去重当作可随意损失的装饰。
- 实现归一化仅去空白和明确标点，不能把任意符号全剥后认定内容相同。
- `build_recall_query` 去当前 user 重复，本地查询不挟带旧话题；不是关闭所有历史指代召回。
- 依据：[conversation_plan.py:269–284](../../../bot/conversation_plan.py#L269-L284)、[qq_openclaw_bridge.py:3330–3357](../../../bot/qq_openclaw_bridge.py#L3330-L3357)。

### 14.3 已有验证声明的来源与边界

- 主控已部署上述修复；本文只记录该事实，不再次部署。
- 主控提供：本地1715 OK、42 skips；线上相关69 OK；五类工程 contracts 全 pass。
- 主控提供：readiness 七项 pass、五项 not_observed；没有将它改写为阶段2.5全通过。
- 主控同解释器 AST 核对频率函数全部不变；线上 settings hash 不变，频率配置逐字节未变。
- 本文没有运行测试，以上是主控已有结果，不是本报告新执行的验证。
- 部署边界重绑只界定当前观察窗口，不删除、补造或洗白历史缺陷。
- 审计依据：[phase_audit.py:563–627](../../../bot/phase_audit.py#L563-L627)、[670–706](../../../bot/phase_audit.py#L670-L706)。

## 15. 阶段 0–5：证据验收、当前判断与缺口

### 15.1 阶段矩阵

| 阶段 | 当前可确认 | 退出证据 | 仍缺什么/禁止怎么说 |
|---|---|---|---|
| 0 统一证据 | 稳定 event/person/action、tool_result、receipt、outbox 已有代码 | 当前窗口全部动作有真实起因、身份归属、匹配终态结果；无悬挂依据 | 有表或累计动作多不等于覆盖通过；五项真实样本未观测仍阻断 |
| 1 自传体记忆 | episode/belief/evidence/revision/self_view 与显式 belief 路径存在 | 能给出什么真实事件改变了观点；非legacy信念和自我修订均有源 | 旧 legacy 体量不能充新证据；不能只凭整理时间认定来源成立 |
| 2 情感影子 | v2 running、积压0、ready_for_review true 的运行快照 | 非空可复算轨迹、源重评、四维与聊天工具覆盖、恢复及心跳可核对 | 观察通过不是心理有效，不得称表达已经受影子控制 |
| 2.5 准入 | 十二项审查，当前主控结果7 pass/5 not_observed，工程五契约pass | 所有准入项pass，真实 organic 样本与工程契约分开成立 | not_observed 不是通过；即使全pass也只是 review_ready |
| 3a 拟议灰度 | 未授权、未接管 | 状态与表达同因；统一注入；重启/换模型连续、延迟实测、可撤回 | 当前只能 blocked 或 review_required，不能宣布自动接管 |
| 3b 拟议扩大 | 当前固定 blocked | 通过灰度评审后再论扩大，保留隐私和能力硬约束 | 不存在 ready 自动放权路径 |
| 4 拟议目标接管 | 既有求知/刷片/私聊/签名 worker，不等于统一接管 | 每目标有归属、终点、预算、放弃条件、结果确认与等待候选 | 统一预算/仲裁未完成，不把 worker 数量当自主性验收 |
| 5 拟议持续修订 | 修订 API/版本表存在 | 长期支持与反例、撤证后再评估、可解释版本轨迹 | 反思不能证明自己；有自我描述不等于长期修订能力通过 |

- 路线证据：[人格内核路线.md:308–341](../../人格内核路线.md#L308-L341)；当前准入口径：[phase_audit.py:563–707](../../../bot/phase_audit.py#L563-L707)。
- 路线中 2026-11-07 生产计数仅是历史快照，不能覆盖 2026-11-10 主控观察，也不能反向填充当前缺失证据。
- AGENTS 的“0/1已落地”应解释为实现基础，不能偷换为每项生产验收已完整通过。

### 15.2 2.5 的十二项检查如何计数

| 检查族 | 通过要求 | 本轮解释 |
|---|---|---|
| ledger_integrity | 当前 cognition 完整性与完整 shadow 完整性 | 与 readiness 分开，不能因阶段部分实现而放权 |
| action_evidence | 当前动作 total>0 且全部现存依据 | 没动作样本为 not_observed，不是空集通过 |
| action_identity | 全部动作 topic 与 private recipient/group scope 匹配 | 结构覆盖仍不是语义授权 |
| action_results | 终态>0 且每个有匹配结果观察 | 没终态样本不构成通过 |
| belief_grounding | 非legacy>0 且全有现存证据 | 明确失效信念被排除，不伪装已修复 |
| self_view_grounding | 修订>0、直接来源全覆盖、无悬挂引用 | consolidation 时间戳不是直接来源 |
| shadow_review | ready_for_review 且没有 expression_control | 影子观察达标，不授权接管 |
| 五类工程契约 | consent/outbox/retraction/version/model-switch 独立证据 | 主控全pass；不能替代上述真实 organic 样本 |

- 上述合计12项；主控当前7 pass/5 not_observed，仍需自然发生的真实证据，不制造活动凑齐。
- 工程检查集外缺项会是 not_verified；源库不可读等为 unknown；非pass 全部进入 blockers。
- 可信部署边界使用 `max(滚动窗口起点,validated deployed_at)`，只划当前观察范围。
- 历史完整性和历史覆盖留在 history_checks/baseline；边界变化不是删除历史或重写账本。
- 全pass时 phase25=review_ready、3a=review_required；3b=blocked、authorized=false、expression_control=false 仍固定。
- 依据：[phase_audit.py:572–611](../../../bot/phase_audit.py#L572-L611)、[677–706](../../../bot/phase_audit.py#L677-L706)。

### 15.3 推进顺序与消融验收

1. 先补真实自然产生的动作归属/结果、非legacy信念依据与自我修订证据，不伪造历史补齐表格。
2. 再评审影子证据口径、参数与词表版本；replay精确只能证明计算一致，不能证明参数合适。
3. 若拟进入3a，先定义统一状态读接口、替换旧随机节奏范围、撤回路径和灰度评审，而非并行增加第二套同职责驱动。
4. 保留 consent、隐私、收件人绑定、工具完成和 outbox 的硬约束；影子控制不应绕过这些门。
5. 用“只有提示词→加记忆→加持续状态→加目标”消融；观察记忆选择、目标选择、等待与行动是否可解释改变。
6. 验证重启/换模型连续性、拒绝/unknown/撤证/回退及首气泡延迟；没有观测就写未证明。
7. 阶段4先低风险检索/整理/草稿，再评估外部私聊与签名；阶段5要求长期反例和版本轨迹，不承诺日期。
8. 依据：[人格内核路线.md:308–343](../../人格内核路线.md#L308-L343)、[AGENTS.md:25–31](../../../AGENTS.md#L25-L31)。

## 16. 真正缺口与交付边界

| 缺口 | 当前证据边界 | 下一步验收所需，不代表本轮要执行 |
|---|---|---|
| 浏览器视觉验收 | BrowserConnector未连接，只有语义/几何/source验证 | 实际浏览12视图，检查缩放、导航、标签和可读性 |
| 真实准入样本 | readiness仍5项not_observed | 等待自然事件并按当前窗口审计，不用测试/候选回放补数 |
| 阶段3统一表达 | expression_control=false、stage3_ready=false | 人工灰度评审、统一注入、替换同职责旧节奏、可撤回 |
| 长期因果效果 | 精确复算与观察覆盖不是心理有效性 | 预先固定消融、连续性、真实行为及误判评估 |
| 全域证据溯源 | 旧群事实/JSON摘要非全字段溯源 | 评估增量迁移和撤证重算边界，不伪造旧来源 |
| 视觉跨群可见性 | group笔记目前可跨群消费 | 单独评估用户预期、scope与回归；本文不改配置/实现 |
| 统一自主仲裁 | 多worker并列，预算/机会局部实现 | 清晰统一提案/归属/预算/结果接口，保留等待 |
| 本地MAA云联动 | P1本地，P2拟议 | 明确远程边界与授权协议后另行设计；不是直接画一条已实现边 |
| 线上延迟与长期稳定 | 本轮无新增首气泡或长期统计 | 实测而非以配置预算代替时延成绩 |

- 本文仅将已完成图 facts、源码锚、主控运行快照和本轮修复信息组织为可审计架构说明。
- 图册、说明与运行快照是本地交付，不公开到网站；会话修复经本地全量测试、独立审查和港机相关测试后发布。未提交或推送 git，未改 admin 账号、现有记忆内容或回复频率配置。
- 最终界线：代码结论不是生产实测；运行快照不是长期保证；观察评审不是接管授权；计算状态不是心理测量或意识证明。
