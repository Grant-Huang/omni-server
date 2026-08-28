# omni-server

为 [`workforce`](https://github.com/Grant-Huang/workforce) 语音输入客户端提供服务的服务端项目。

**当前状态**：阶段 1（单用户）的服务端骨架已经落地并有测试覆盖，记忆能持久化（SQLite，重启不丢）；还没有接过真实上游，见 [`docs/roadmap.md`](docs/roadmap.md) 的待做清单。

## 要做成什么样

- **多用户 + 群组**：既服务单个用户，也服务由多个用户组成的群（比如一个家庭）。一个人可以同时在很多个群里。
- **实时语音会话**：延迟目标是对话级别（用户说完到 AI 开口 1 秒左右）。
- **服务端工具**：查询、分析、记忆、文档知识库。**不依赖 Realtime 的 Function Calling**——工具跑在一个并排的快速文本模型上，结果注入回实时会话，同时把结构化结果推给 App。
- **统一会话 AI 做路由**：用户不在客户端选择群，只需要说"我要跟 A 说话"或"我要在 BB 群里跟 A 说话"。
- **分层记忆**：人格、规则、画像、事务、日常、群共享、一过性七层，按层注入、按层给预算。

## 跑起来

```bash
pip install aiohttp websockets python-dotenv
QWEN_API_KEY=... QWEN_WORKSPACE_ID=llm-xxxx python3 -m omni.server
```

强烈建议配 `QWEN_WORKSPACE_ID`：共享域名会静默吞掉 `session.update`，workforce 在这上面花了好几天（见 `omni/upstream.py` 的注释）。没配会回退到共享域名并给出警告。

跑测试（全部离线，不打上游，不消耗配额）：

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

## 文档

- [`docs/design-risks-review.md`](docs/design-risks-review.md) — 动手之前的风险盘点：十条风险、该先跑的实验。
- [`docs/memory-design.md`](docs/memory-design.md) — **记忆专题**：分层为什么是三个正交属性、个人记忆和群记忆「打通」到底现实不现实、人格层的写权限为什么是安全边界、自动 supersede 为什么默认要关。
- [`docs/architecture.md`](docs/architecture.md) — **架构方案**：组件边界、数据结构（含 SQLite 持久化设计）、部署约束、技术选型记录。
- [`docs/roadmap.md`](docs/roadmap.md) — 分阶段落地计划，阶段 1 的验收标准。

## 相关仓库

- [`Grant-Huang/workforce`](https://github.com/Grant-Huang/workforce) — 语音输入客户端（网页版）。它的 `web-demo/README.md`、`docs/app-design.md`、`docs/roadmap-todo.md` 里有大量对接 Qwen-Omni-Realtime 的实测记录（记忆注入方式、限流行为、热词支持情况、`modalities` 行为等），是 omni-server 设计的主要输入，不要重新踩一遍。
