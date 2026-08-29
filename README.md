<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo-light.svg" alt="家里 · JIA Family AI" width="280">
  </picture>
</p>
<p align="center"><em>服务端</em></p>

# omni-server

「家里（JIA Family AI）」——一个属于全家人的 AI，通过实时语音、照片和生成式 AI，让一家三代看见彼此、连接彼此，把共同生活变成可以传承的家庭记忆。这是它的服务端，为客户端 [`omni`](https://github.com/Grant-Huang/omni) 提供服务。

**当前状态**：阶段 1（单用户）的服务端骨架已经落地并有测试覆盖，记忆能持久化（SQLite，重启不丢），`omni` 客户端（`web-demo/`）已经接上（2026-08-29）——`/ws`+`/api/config`+CORS 都验证过（浏览器⇄omni-server⇄脚本化假上游，全链路跑通）；还没有接过真实 DashScope 上游，见 [`docs/roadmap.md`](docs/roadmap.md) 的待做清单。

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

## 部署：这个仓库其实是两个独立的部署单元

`omni/server.py`（这个 Python 后端）和 [`marketing-site/`](marketing-site/)（宣传首页/客服中心/下载页）**只是放在同一个 git 仓库里，不是同一个部署单元**：

- **`omni/server.py`**：常驻进程，管实时语音 WebSocket 中转、拿着 API Key、读写 SQLite 记忆。必须部署成一个跑着的服务（云主机/容器），不能扔静态托管。
- **`marketing-site/`**：纯静态 HTML/CSS，跟这个 Python 服务没有任何调用关系，独立部署到任意静态托管（Netlify/Vercel/GitHub Pages/自建 Nginx 都行）。具体步骤见 [`marketing-site/README.md`](marketing-site/README.md)。

之所以放在同一个仓库，只是因为「宣传/客服/下载页」这三个页面归属服务端团队维护，跟代码在哪个 git 仓库里没有强绑定关系；千万不要把 `marketing-site/` 接进 `server.py` 的路由里——宣传页的发布节奏、流量模式、安全要求都跟拿着 API Key 的语音后端不一样，混在一起既没必要也不划算。

对应地，客户端一侧（用户实际长期使用的产品界面）**始终在 [`omni`](https://github.com/Grant-Huang/omni) 仓库的 `app/`**（2026-08-29 从 `mobile-demo/` 改名——这是要发布上线的产品，不再是 demo），不会因为它是"前端"就挪到这边来。

## 文档

- [`docs/design-risks-review.md`](docs/design-risks-review.md) — 动手之前的风险盘点：十条风险、该先跑的实验。
- [`docs/memory-design.md`](docs/memory-design.md) — **记忆专题**：分层为什么是三个正交属性、个人记忆和群记忆「打通」到底现实不现实、人格层的写权限为什么是安全边界、自动 supersede 为什么默认要关。
- [`docs/architecture.md`](docs/architecture.md) — **架构方案**：组件边界、数据结构（含 SQLite 持久化设计）、部署约束、技术选型记录。
- [`docs/roadmap.md`](docs/roadmap.md) — 分阶段落地计划，阶段 1 的验收标准。
- [`docs/family-app-architecture.md`](docs/family-app-architecture.md) — **产品架构框架**：具体产品（家里）落到 omni-server 上要新增哪些模块（照片/生成式回忆/连接建议/家庭作用域）、为什么这些新内容不该塞进 `MemoryEntry`、对前四份文档的具体修订清单。写作时产品的正式名字还没定，文中用的是内部代号「三代纽带」，指的是同一个产品。

## 相关仓库

- [`Grant-Huang/omni`](https://github.com/Grant-Huang/omni) — **当前的客户端**。2026-08-28 从 workforce 分叉出来，专属于「家里」这个产品——以后这个产品的客户端改动提交到这里，不再提交到 workforce。
- [`Grant-Huang/workforce`](https://github.com/Grant-Huang/workforce) — omni 的上游来源，以后作为**通用项目**保留，不再承载这个产品的改动。它的 `web-demo/README.md`、`docs/app-design.md`、`docs/roadmap-todo.md` 里有大量对接 Qwen-Omni-Realtime 的实测记录（记忆注入方式、限流行为、热词支持情况、`modalities` 行为等）——这些是 omni-server 设计时的历史依据，下面几份文档里凡是引用"workforce 测过/记录过"的地方，说的都是这些记录，不用重新踩一遍，也不用因为客户端换了仓库就怀疑这些结论过时。
