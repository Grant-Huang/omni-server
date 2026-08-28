# 架构方案

**这份文档是什么**：把已经讨论、已经落地的决定收拢成一份"系统是怎么搭的"参考——组件边界、数据怎么流、部署长什么样、技术选型的理由。不重复 `design-risks-review.md`（风险）、`memory-design.md`（记忆专题的"为什么"）、`roadmap.md`（分几个阶段做）已经讲过的内容，需要的地方直接引用。

**不覆盖什么**：产品定位、差异化、目标人群——这些按讨论约定，另外找时间聊。这份文档只谈"系统怎么建"，不谈"系统为谁建"。

**2026-08-28 定下的四个技术决策**（下面各节会展开）：持久化用 SQLite，轻量优先，需要时再换；部署目标暂不定，先在当前开发环境跑通，但把硬约束（cn-beijing）写清楚；Python + aiohttp 定为正式方向；身份体系 omni-server 自建，不依赖 workforce/AgentNexus 现有账号。

---

## 1. 系统边界与组件图

```
┌─────────────┐   WebSocket（语音/文字）    ┌──────────────────────────────────────────┐
│ workforce   │ ───────────────────────────▶│              omni-server                  │
│ web 客户端   │◀─────────────────────────── │                                            │
└─────────────┘   omni.* / 透传事件          │  ┌──────────────┐                          │
                                             │  │ VoiceSession │ 事件解析、注入、合流       │
                                             │  │ (realtime.py)│                          │
                                             │  └──────┬───────┘                          │
                                             │         │                                  │
                                             │  ┌──────▼───────┐    ┌─────────────────┐   │
                                             │  │ MemoryStore  │◀──▶│ SqliteMemory     │   │
                                             │  │ (memory.py)  │    │ Persistence      │───┼──▶ omni_memory.db
                                             │  │ + layers.py  │    │ (persistence.py) │   │
                                             │  └──────┬───────┘    └─────────────────┘   │
                                             │         │                                  │
                                             │  ┌──────▼───────┐    ┌─────────────────┐   │
                                             │  │ Sidecar      │    │ Extractor        │   │
                                             │  │ (sidecar.py) │    │ (extraction.py)  │   │
                                             │  └──────┬───────┘    └────────┬─────────┘   │
                                             │         │                    │             │
                                             └─────────┼────────────────────┼─────────────┘
                                                        │                    │
                                          DashScope Realtime WS      DashScope 文本补全
                                        （语音进语音出，qwen3.5-omni-*）  （qwen-turbo）
```

**七个模块，边界按"谁依赖谁"画，不按"谁调用谁的函数"画：**

| 模块 | 依赖 | 不依赖 |
|---|---|---|
| `layers.py` | 无 | 纯数据，任何人都能读 |
| `memory.py` | `layers.py` | 不知道 SQLite 存在——`PersistHook` 是它定义的协议，不是它的实现 |
| `persistence.py` | `memory.py`（只用它的类型） | 不知道 `VoiceSession`/`Sidecar` 存在 |
| `instructions.py` | `layers.py`、`memory.py` 的 `Retrieved` 类型 | 不知道上游协议、不知道 SQLite |
| `sidecar.py` | `memory.py`（复用检索）、`textmodel.py` | 不知道 Realtime 协议 |
| `extraction.py` | `layers.py`、`memory.py`、`textmodel.py` | 不知道 Realtime 协议、不知道 Sidecar |
| `realtime.py` | 上面所有模块 | 不知道 aiohttp、HTTP 路由 |
| `server.py` | 上面所有模块 + `upstream.py` | —— 唯一知道"这是个 WS 服务"的模块 |

这个方向是故意的：**核心逻辑（分层、检索、合流、提炼）不知道自己跑在 aiohttp 里，也不知道自己在跟 DashScope 说话**。70+ 个测试全部离线跑通，就是这个边界画对了的直接证据——`tests/fakes.py` 能完整代替 `upstream.py` 和 `textmodel.py`，是因为业务逻辑从头到尾只认 `Upstream`/`TextModel` 这两个 Protocol，不认具体实现。以后要换传输层（比如从 WebSocket 换成别的）或者换文本模型供应商，改动范围就是 `upstream.py`/`textmodel.py`/`persistence.py` 这几个"知道外面世界长什么样"的模块，业务逻辑不用动。

---

## 2. 数据架构

### 2.1 记忆：`MemoryEntry`

字段来自 `memory-design.md` §7 的结论（溯源/作用域/supersede 必须从第一天就有），SQLite 表结构是它的直接映射（`persistence.py` 的 `SCHEMA`）：

```
memory_entries
  id              TEXT PRIMARY KEY
  text            TEXT
  layer           TEXT        -- persona / policy / profile / task / episodic / shared / ephemeral
  scope           TEXT        -- "user:<id>" 或 "group:<id>"，归属域
  written_by      TEXT        -- human / extraction
  created_at      REAL
  updated_at      REAL
  expires_at      REAL NULL   -- NULL = 永不过期
  superseded_by   TEXT NULL   -- 指向取代它的条目 id
  session_id      TEXT NULL   -- 仅 ephemeral 层使用
  origin_scope    TEXT        -- 内容最初是在哪个域说的（§4 的洗白通道防线）
  speaker_id      TEXT NULL
  source_session_id TEXT NULL
  turn_id         TEXT NULL
  confidence      REAL        -- 说话人归属置信度，见 memory-design.md §8
```

**`ephemeral` 层从不落盘**（`persistence.py` 的 `on_add` 显式跳过）。它的生命周期定义就是"绑定一次会话"，进程重启后不会有任何会话能认领一条历史 `session_id`，落盘只会产生永远打不开的死数据。

**存储层不做业务判断**——SQLite 这边只是"把 `MemoryEntry` 原样存起来、原样读回来"，supersede 判断、TTL 判断、作用域过滤这些全部还是 `memory.py` 的活，重启后通过 `MemoryStore.restore()` 把数据摆回内存，之后走的还是同一套已经测过 76 次的逻辑。**这是刻意的分工**：业务规则只写一遍、只测一遍，SQLite 只负责"别丢"。

### 2.2 命名空间：`scope` 字段

`user:<id>` / `group:<id>` 两种前缀，字符串形式，不是外键。v0 只写 `user:` 这一种，`group:` 的读路径（`disclosable`、检索的 `memberships` 参数）已经实现并测过，只是没有写入口——群功能上线时是**加一个写入路径**，不是重新设计数据模型。

### 2.3 身份：自建，阶段 2 落地，这里先定形状

**决策**：不复用 workforce/AgentNexus 现有账号体系（目前也没有能复用的），omni-server 自己管用户和凭证。理由很直接——语音场景的连接模式是"建立一次、开很久"，不是"每次请求都带会话"，这跟 workforce 给智枢提案里"建议一"讨论过的问题是同一个：短期 JWT 不适合无人值守的长连接，需要的是长期凭证（那份提案里叫 `pt_...` token），这里直接复用同一个思路，不重新发明。

最小闭环（阶段 2 实现，这里只定接口形状，不写代码）：

```
users
  id            TEXT PRIMARY KEY     -- 就是 user_scope 去掉 "user:" 前缀
  display_name  TEXT
  created_at    REAL

credentials
  token_hash    TEXT PRIMARY KEY     -- 只存哈希，同 workforce 提案里 PersonalToken 的设计
  user_id       TEXT REFERENCES users(id)
  created_at    REAL
  revoked_at    REAL NULL
```

认证流程绕开一个具体的坑：**浏览器原生 WebSocket API 不能设自定义 header**，这正是 workforce 的 `server.py` 当初要做中转层的原因（`Authorization: Bearer` 没法直接带上）。所以流程是：

1. 客户端先走一次普通 HTTPS 请求换 token（`POST /api/auth/token`，v0 阶段可以就是一个手工生成的邀请码换长期 token，不需要账号密码体系）。
2. 建 WS 连接时，token 作为 upgrade 请求的查询参数带上（`wss://.../ws?token=...`）。
3. **查询参数会进访问日志**，这是要注意的点——`server.py` 的 access log 中间件需要显式脱敏这个参数，或者改成"upgrade 成功后的第一条消息带 token，服务端限时校验，超时未验证就关闭连接"这个变体（多一次消息往返，但不进日志）。这个取舍留到阶段 2 实现时定。
4. `Config.user_scope` 从硬编码的 `"user:local"` 改成从校验通过的 token 查出来的 `f"user:{user_id}"`——这是唯一要动的地方，`VoiceSession`/`MemoryStore`/`Sidecar` 全部已经是按 `user_scope` 参数化的，不用因为加身份体系而改。

---

## 3. 关键流程

### 3.1 一轮对话：注入 → 应答 → 查询合流

已经在 `memory-design.md` §9 详细展开，这里给一个贯穿全部模块的时序视图：

```
用户说完 → transcript
   │
   ├─(A)─▶ VoiceSession._patch_instructions()
   │         └─▶ MemoryStore.retrieve(query, output_scope, memberships)
   │               └─▶ instructions.build()：按层预算拼接，稳定层在前
   │         └─▶ 与上次发送的 instructions 比较（InstructionPatcher）
   │               ├─ 相同 → 跳过，不发 session.update
   │               └─ 不同 → 发 session.update，等 session.updated ack（≤4s 超时兜底）
   │
   ├─(B)─▶ VoiceSession._create_response() → response.create
   │         （turn.opened 置位——这是屏障，见下）
   │
   └─(C)─▶ Sidecar.run()（与 A/B 并行）
             └─▶ 文本模型判断要不要查 → 要就调用对应 Tool → 结果回来
                   └─▶ 等 turn.opened（屏障：结果可能比 response.create 先回来）
                   └─▶ 按 turn.response_in_flight 和已说时长，三选一合流：
                        - 早（<2.5s）→ 自打断：response.cancel + 通知客户端丢弃缓冲音频
                                      + 重新 patch（带上查询结果）+ response.create
                        - 中（已说一会）→ 排队，等 response.done 后追加说
                        - 晚（已说完）→ 直接追加
                   └─▶ 结构化结果始终推给 App（omni.tool_result），不管语音是否被打断
```

**A、B 之间没有依赖**（都是每轮固定要做的事），**C 独立并行**，三条路径在"合流"这一步才汇合。这个并行结构是能满足 1 秒延迟目标的核心——如果做成"先查、查完再答"的串行结构，延迟直接是两段之和。

### 3.2 会话结束：批量提炼

```
WS 连接关闭
   │
   └─▶ server.py 的 finally 块
         ├─▶ VoiceSession.close()：丢弃一过性记忆（forget_session），关闭上游连接
         └─▶ asyncio.create_task(_extract(...))：不阻塞连接关闭本身
               └─▶ Extractor.extract(整段对话, existing_hint=近期记忆)
                     └─▶ 文本模型一次性判断：分层、TTL、是否 supersede 已有条目
                     └─▶ MemoryStore.add()（写权限检查在这里生效：extraction 写不进
                          persona/policy）
                           └─▶ PersistHook.on_add()/on_supersede() → SQLite
```

提炼失败不影响任何已经发生的事——连接已经关闭，用户已经拿到了对话内容，这一步纯粹是"要不要把这次对话沉淀成长期记忆"，失败了记录日志，不重试（阶段 2 视情况加重试）。

---

## 4. 部署

### 4.1 硬约束（不随部署目标变化）

- **必须在 cn-beijing 区域**。专属域名 `{WorkspaceId}.cn-beijing.maas.aliyuncs.com` 钉死地域，部署在别处每个音频包、每次 `session.update` ack 都要多付一次跨区往返（`design-risks-review.md` §5）。
- **必须是长驻进程，不能是按请求计费的无状态函数**。WS 连接是长连接（最长 120 分钟一条对话），`asyncio.Task` 形式的 sidecar/extraction 后台任务、进程内的 `SESSIONS` 集合，都要求进程在整个会话期间存活。
- **需要能写本地磁盘或挂载持久卷**（SQLite 文件），或者在换成托管数据库之前接受"换实例就丢数据"。
- **出方向要能连 DashScope 的 wss/https**。

### 4.2 现状

**部署目标还没定，先在当前开发环境里把端到端跑通**——这是今天定的优先级：与其在没有真实流量特征之前猜测容量、猜测该用 ECS 还是容器编排，不如先把单用户全链路跑通、量出真实的延迟和资源数字，再拿着这些数字去选部署形态。当前跑法：

```bash
QWEN_API_KEY=... QWEN_WORKSPACE_ID=llm-xxxx OMNI_DB_PATH=omni_memory.db python3 -m omni.server
```

单进程，SQLite 文件在本地磁盘，没有反向代理/TLS 终端（本地开发用 `ws://`，生产前要补 TLS）。

### 4.3 什么时候必须选型

留几个明确的触发点，而不是"以后再说"：

- **要接真实用户流量之前**：至少需要一个能长驻、能配 TLS、能配置出方向白名单到 DashScope 的地方——普通云主机（ECS 一类）就够，不需要编排。
- **要支持第二个并发用户之前**（阶段 2）：SQLite 的单文件写锁在低并发下没问题，但"能不能扛住 N 个用户同时写"这件事需要实测，测不过再换 Postgres——`persistence.py` 的模块边界就是为了让这次替换只改一个文件。
- **要水平扩容之前**（多实例）：两个东西现在是单实例假设，扩容前必须先解决——① `server.py` 的 `SESSIONS` 是进程内 `set()`，多实例下每个实例只看到自己那部分连接；② SQLite 是单文件，多进程共享同一个文件在写并发上会成为瓶颈。这两个解决方案通常是同一个：换成能被多实例共享的数据库（Postgres），`SESSIONS` 如果需要跨实例可见（比如"用户在哪个实例上"这类路由信息）就单独用 Redis 一类的共享状态存。

---

## 5. 技术栈决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 语言/框架 | **Python + aiohttp**，定为正式方向 | 已有原型验证过；aiohttp 的 async 模型天然适合"持有大量并发 WS 连接、每条连接内部还要并行跑 sidecar 查询"这个负载形状；没有明显性能短板需要现在就换语言 |
| 持久化 | **SQLite**，MVP 阶段 | 零运维、单文件、stdlib 自带；`PersistHook` 协议把替换面收窄到一个文件（`persistence.py`），需要 Postgres 时不动业务逻辑 |
| 部署目标 | **暂不定**，先在开发环境跑通 | 没有真实流量特征之前，选具体云厂商/编排方案的信息不够；硬约束（cn-beijing、长驻进程）已经写进 §4.1，供以后选型时对照 |
| 身份体系 | **自建**，阶段 2 实现 | 没有现成账号系统可复用；复用 workforce 给智枢提案里已经讨论过的长期 token 思路，不重新发明 |
| 工具调用 | **并排文本模型**，不依赖 Realtime Function Calling | 见 `design-risks-review.md` 的 R3 决策更新，`memory-design.md` §9 |
| 自动 supersede | **默认关闭** | 见 `memory-design.md` §6，中文场景下相似度信号不可靠，测出来的结论 |

---

## 6. 非功能需求

延迟预算的完整分解在 `design-risks-review.md` §8，这里只列跟本文档的架构选择直接相关的几条：

- **`instructions` 拼接 + 每层预算裁剪**必须在个位数毫秒内完成——纯内存操作，`instructions.py` 没有任何 IO，这条不需要专门优化，只需要不引入意外的 IO（比如以后加语义检索时，检索本身可能有 IO，要单独控制在预算内，见 `memory-design.md` §10）。
- **SQLite 写入在关键路径之外**：`PersistHook.on_add`/`on_supersede` 只在 `Extractor`（会话结束后）和 `POST /api/memory`（人工写入，本来就不是实时对话路径）里被调用，从不在 `VoiceSession` 的每轮流程里出现。这是刻意的——如果分层记忆的写入路径不小心跑进了每轮对话的关键路径，SQLite 的同步写就会变成延迟预算里意外的一项。
- **进程重启的可观测性**：`make_app` 在恢复记忆后打一行 INFO 日志（`restored N memory entries from <path>`），这不是装饰性日志——它是"持久化真的生效了"这件事在生产环境唯一免费的信号，出问题时第一个要看的地方。

---

## 7. 与其他文档的关系

```
design-risks-review.md   风险清单 + 该跑哪些实验（R1...R10, E1...E7）
        │
memory-design.md         记忆专题：分层为什么这样分、打通为什么这样打通
        │
architecture.md（本文档） 系统怎么搭：组件边界、数据结构、部署、技术选型
        │
roadmap.md                分几个阶段做、每个阶段的验收标准
        │
family-app-architecture.md   具体产品（「三代纽带」）落到这套架构上要新增什么、
                              修订什么——上面四份是通用地基，这份是长在地基上的具体楼
```

五份文档大致是"为什么会有这些坑 → 记忆这块具体怎么设计 → 整个系统怎么搭 → 分几步做 → 具体产品怎么落地"的顺序，交叉引用，不互相重复内容。
