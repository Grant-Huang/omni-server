# omni-server

为 [`workforce`](https://github.com/Grant-Huang/workforce) 语音输入客户端提供服务的服务端项目。

**当前状态：单用户 MVP，能跑。** 群/多用户是下一阶段（先做单用户，群的设计留到那时候再定，见 `docs/mvp-plan.md`）。

## 要做成什么样

- **多用户 + 群组**（下一阶段）：既服务单个用户，也服务由多个用户组成的群（比如一个家庭）。一个人可以同时在很多个群里。
- **实时语音会话**：人和 AI 实时语音对话，延迟目标是对话级别（用户说完到 AI 开口 1 秒左右）。
- **服务端工具**：查询、分析、记忆、文档知识库等能力放在服务端，而不是客户端各做一套。
- **统一会话 AI 做路由**（下一阶段）：用户**不在客户端选择群**。他只需要对统一会话 AI 说"我要跟 A 说话"，AI 就把话转给 A；说"我要在 BB 群里跟 A 说话"，AI 就在 BB 群里发一条 @A 的消息。
- **分层记忆**（下一阶段）：记忆区分人格层、规则层、日常记忆、事务性、一过性等，使用时按层注入，而不是一股脑塞进去。

MVP 阶段做的是骨架：`客户端 ⇄ omni-server ⇄ DashScope` 这条链路本身，把 workforce `web-demo/server.py` 那个本地联调脚本变成一个可以独立部署、可以自动化测试的真实服务。记忆注入、路由这些还是客户端在做，没有搬过来——为什么这么排序见 `docs/mvp-plan.md` 第 1 节。

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入 QWEN_API_KEY，建议同时填 QWEN_WORKSPACE_ID（见 .env.example 里的说明）
.venv/bin/python -m server
```

默认监听 `127.0.0.1:8766`。跑测试：`.venv/bin/python -m pytest`（不需要真实 Key——用一个脚本化的假上游测 `/ws` 转发逻辑，见 `docs/mvp-plan.md` 第 4 节）。

要让 `workforce/web-demo` 这个前端连过来，看它 README 里"对接 omni-server"那一节。

## 文档

- [`docs/mvp-plan.md`](docs/mvp-plan.md) — **单用户 MVP 的开发计划**：这版做什么、明确不做什么、为什么这么排序、怎么在没有真实 Key/麦克风的情况下测试。
- [`docs/design-risks-review.md`](docs/design-risks-review.md) — **动手之前的风险盘点**：这个使用模式有哪些坑、哪些"看起来想清楚了"的地方其实站在没验证过的假设上、建议先跑哪几个实验。依据是 workforce 仓库过去两周积累的真实实测记录。

## 相关仓库

- [`Grant-Huang/workforce`](https://github.com/Grant-Huang/workforce) — 语音输入客户端（网页版）。它的 `web-demo/README.md`、`docs/app-design.md`、`docs/roadmap-todo.md` 里有大量对接 Qwen-Omni-Realtime 的实测记录（记忆注入方式、限流行为、热词支持情况、`modalities` 行为等），是 omni-server 设计的主要输入，不要重新踩一遍。
