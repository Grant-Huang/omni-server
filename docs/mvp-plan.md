# omni-server 开发计划：单用户 MVP

**这份文档是什么**：接着 `docs/design-risks-review.md` 的风险盘点，落到一版可以动手写代码的计划——MVP 做什么、明确不做什么、按什么顺序做、怎么验证。对应的决定：**先做单用户，群/多用户之后再做**（如果群的设计后来发现不合理，到时候再改）。

**前端**：不重新设计 UI。直接复用 `workforce` 仓库 `web-demo/` 已经做好、也已经实测过语音链路的前端（ChatGPT 风格主对话框 + 语音大按钮 + 打字输入）。这次改动只碰前端的"连哪个后端"这根线，不碰界面。

---

## 1. MVP 的边界：走通骨架，不做记忆搬家

`design-risks-review.md` 里"服务端工具/记忆放服务端"是产品终态愿景，但把它整个搬到 MVP 里做，风险和工作量都不匹配单用户阶段该有的投入。单用户场景下，`web-demo` 现有的**客户端本地记忆**（`memory.js` 关键词+新鲜度检索）已经是经过实测、能跑的方案——R4（不可信内容注入）、R9（并发写）这些逼着记忆非搬到服务端不可的风险，全部是"多用户/群"才出现的。所以 MVP 不做这次搬家，把它留到进入群阶段、真正需要跨设备/跨成员共享记忆的时候再做（那时候也顺带解决 R2 提到的 BYOK/配额问题）。

**MVP 要证明的是骨架本身走得通**：`客户端 ⇄ omni-server ⇄ DashScope` 这条链路，omni-server 作为一个独立部署的服务而不是本地脚本，能不能稳定接住 `web-demo` 已经验证过的协议（`session.update` instructions patch 记忆注入、`session.updated` ack 等待、8-23 那次专属域名切换后验证过的多轮稳定性）。骨架先跑通、先能测，后面加记忆/工具/路由才有地基。

### 1.1 MVP 范围内

| # | 内容 | 对应 design-risks-review.md |
|---|---|---|
| M1 | `/ws` 中转：omni-server 持有到 DashScope 专属域名的连接，转发双向事件，不认识的事件类型原样透传 | Section 5(b) |
| M2 | `/api/config`：把 `web-demo/server.py` 现有的音色列表/hasKey 探测原样搬过来 | — |
| M3 | `/api/dictation-cleanup`、`/api/memory-extract`：现有的两个一次性 `qwen-turbo` 调用原样搬过来（口述整理、记忆提炼），本来就该在服务端 | — |
| M4 | `/api/memory`：新增的持久化单用户记忆存取接口（增/搜，算法照抄 `memory.js` 的关键词重合+新鲜度），**先落地接口和测试，先不接入 `/ws` 的注入流程** | 为 7.x 打地基，但不做 7.x 全部内容 |
| M5 | 部署形态：单进程 aiohttp 服务，配置走 `.env`（Key 走服务端配置，不是 BYOK——BYOK 留给群阶段的 R2 讨论） | Section 4/5(a) |
| M6 | 前端接线：`web-demo` 的后端地址从"写死同源"改成可配置，可以指向部署好的 omni-server | — |

### 1.2 明确不做（写下来是为了不被中途顺手加进去）

- **不做记忆服务端注入**：`/ws` 转发这一路暂时是纯透传，不解析事件内容、不做 instructions patch。客户端继续用它自己已经验证过的 `LocalMemory` + `SaveIntent` 流程。`/api/memory` 存在但暂时没人调用它做实时注入。
- **不做群/路由**（R5、R6、Section 6 整节）——这是明确要"过程中再讨论"的部分，不在这版计划里。
- **不做服务端工具/Function Calling**（R3）——E2 实验还没跑，跑之前不建任何依赖它的架构。
- **不做 BYOK / 多主账号调度**（R2）——单用户不会撞配额墙，这版不需要解。
- **不做说话人识别**（R10）——只有共享设备场景才需要，单用户 MVP 用不上。
- **不做 AgentNexus 集成**——`agentnexus.js` + `agentnexus_mock.py` 是给"智枢"这个另外的系统对接用的 mock，跟 omni-server 是两条不同的线，这版不碰。
- **不做记忆分层/supersede/溯源**（Section 7 全部细节）——`/api/memory` 这版只是一个能存能搜的最简单实现，分层预算、TTL、供 superseded_by 这些留到真正把记忆搬到服务端、且需要支撑多群多用户的那一步。

---

## 2. 架构决定

### 2.1 `/ws` 先做透传，不做"有状态代理"

`design-risks-review.md` Section 5(b) 说 omni-server 终态是要解析事件、抓转写、决定何时注入的"有状态代理"，不能是纯字节转发。MVP 阶段刻意退一步，先做纯转发（跟 `server.py` 现在的 `relay()` 一样的转发逻辑），原因：

- 单用户阶段，记忆注入放客户端已经够用（1.1 的论证），没有转发之外的强需求。
- 纯转发的正确性容易验证（有一份可回放的协议记录，见第 4 节），先把这条链路的可靠性做扎实，再往上叠解析逻辑，比一次性做"连接+解析+注入"整个成本低。
- `/api/memory` 提前把接口和存储做出来但不接入转发路径，是为了让"往有状态代理演进"这一步在后面只是"在转发循环里插一段调用"，不用重新设计存储层。

### 2.2 不做 BYOK，但把 Key 配置做成可替换的

服务端一份 `.env` 配置（`QWEN_API_KEY`/`QWEN_WORKSPACE_ID`/`QWEN_MODEL`/`QWEN_VOICE`），跟 `server.py` 现在的做法一样，只是从"本地脚本"变成"可部署的服务"。配置读取集中在一个 `config.py` 里，不散落在各处——这样将来要支持"每个家庭自己的 Key"时，改的是配置的来源（从 `.env` 换成按连接查库），不用改用到配置的地方。

### 2.3 CORS：前端和后端从此不同源

`web-demo` 现在图简单，前端页面和 `/ws`/`/api/*` 都是 `server.py` 同一个进程 serve 的，同源不用管 CORS。omni-server 是独立部署的服务，前端网页可能跑在别的域名/端口下，所以：

- HTTP API（`/api/config`、`/api/dictation-cleanup`、`/api/memory-extract`、`/api/memory`）需要 CORS 头，允许的源通过 `.env` 里的 `CORS_ORIGINS`（逗号分隔）配置，默认给本地开发用的 `*`（生产环境部署时要求显式配置，不能留 `*`）。
- `/ws` 本身走 WebSocket 握手，浏览器不对 WS 做 CORS 预检那一套，但同样要注意：MVP 阶段 `/ws` 不做任何鉴权（单用户内部联调够用），公网部署前必须补一个最基本的鉴权（哪怕只是一个共享 token），这条写进"MVP 之后要做的第一件事"，不能忘。

### 2.4 前端改动的边界

只改"往哪连"，不改"UI 长什么样、交互怎么走"。具体是三处 WebSocket 构造（语音会话、文字会话、口述转文字）和三处 `fetch("/api/...")` 调用，从硬编码 `location.host`/相对路径改成读一个可配置的 base（默认值让现有行为完全不变，`server.py` 本地联调这条路完全不受影响）。`app.js` 里客户端记忆管理（`handleUserTurn` 里 `LocalMemory.search`/`SaveIntent`/`AgentNexusBridge` 那部分）这次**不动**——按 2.1 的决定，这次不做记忆搬家。

---

## 3. 里程碑顺序

1. **M1+M2**：`/ws` 透传 + `/api/config`。这是骨架，先跑通，先能用 mock 上游测。
2. **M3**：口述整理、记忆提炼两个一次性调用搬过来。跟 M1 无依赖，可以并行。
3. **M4**：`/api/memory` 存取接口 + 测试。跟 M1/M3 都无依赖，可以并行。
4. **M5**：配置、CORS、部署相关的收尾（`.env.example`、`.gitignore`、启动脚本、README）。
5. **M6**：前端接线（`workforce` 仓库那一半）。依赖 M1/M2/M3 的路由已经定下来（前端要知道具体请求哪些路径）。

M1-M4 之间没有强依赖，实现时会放在同一批提交里一起做完，不特意拆多次提交制造没必要的中间状态。

## 4. 怎么测（没有真实 Key、没有真实麦克风的前提下）

跟 `design-risks-review.md` 第 12 节"可测试性"建议的方向一致：**不依赖真实 DashScope 连接**做单元/集成测试。做法是写一个最小的"假上游"WebSocket 服务器，按已经实测确认的协议子集（`session.update`→`session.created`/`session.updated`、`conversation.item.create`、`response.create`→`response.audio_transcript.delta`/`response.done`）脚本化应答，omni-server 的 `/ws` 转发逻辑对着这个假上游测试：

- 正常转发：客户端发的事件原样到达假上游，假上游发的事件原样回到客户端。
- 未知事件类型：假上游发一个测试没见过的新事件类型，验证客户端能收到（不会被吞）。
- 错误路径：`QWEN_API_KEY` 未配置时返回 `relay.error`；上游连接失败时返回 `relay.error` 并带上具体原因。
- `/api/memory` 的增/搜逻辑：用固定的输入数据验证打分排序符合预期（照抄 `memory.js` 的算法，需要保证移植后行为一致）。
- `/api/dictation-cleanup`、`/api/memory-extract`：mock 掉实际的 HTTP 调用（`aiohttp.ClientSession.post`），验证请求体组装正确、各种错误状态码（超时/非 200/非 JSON）处理正确——这两个接口本身的逻辑是从 `server.py` 原样搬过来的，已经在 `web-demo` 里实测过，这里测的是"搬过来之后没搬错"，不是重新验证 Qwen 那边的行为。

这些测试**验证的是 omni-server 自己代码的正确性**，不是"这次真的连上 Qwen 了"。真实连通性验证仍然需要在有真实 Key 的环境里手动跑一次（跟 `web-demo` 当初的验证方式一样），MVP 交付时会在 README 里写清楚这一步还需要人工做，不能假装自动化测试替代了它。

## 5. MVP 之后，进入群阶段之前，第一件事要做什么

MVP 跑通之后如果继续往前走（还没到群/多用户），建议的下一步顺序：

1. 补一个最基本的 `/ws` 鉴权（哪怕只是共享 token）——MVP 阶段为了单用户内部联调简单先跳过，但只要考虑部署到公网就不能再跳过。
2. `design-risks-review.md` 里的 E1（复测记忆注入方式）、E2（Function Calling）——这两个决定架构，且和"先做单用户还是先做群"无关，什么时候做都不亏。
3. 把记忆注入接进 `/ws` 转发路径（用上 M4 已经做好的 `/api/memory`），同时把客户端 `handleUserTurn` 里那段本地记忆管理去掉——这一步才是真正把"记忆放服务端"这条产品主线落地，MVP 里刻意没做，是留给这里。

这之后再进入群阶段，对应 R2/R4/R5/R6/R9/R10 那一整套设计。
