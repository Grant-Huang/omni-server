# omni-server

为 [`workforce`](https://github.com/Grant-Huang/workforce) 语音输入客户端提供服务的服务端项目。

**当前状态：设计阶段，还没有代码。**

## 要做成什么样

- **多用户 + 群组**：既服务单个用户，也服务由多个用户组成的群（比如一个家庭）。一个人可以同时在很多个群里。
- **实时语音会话**：人和 AI 实时语音对话，延迟目标是对话级别（用户说完到 AI 开口 1 秒左右）。
- **服务端工具**：查询、分析、记忆、文档知识库等能力放在服务端，而不是客户端各做一套。
- **统一会话 AI 做路由**：用户**不在客户端选择群**。他只需要对统一会话 AI 说"我要跟 A 说话"，AI 就把话转给 A；说"我要在 BB 群里跟 A 说话"，AI 就在 BB 群里发一条 @A 的消息。
- **分层记忆**：记忆区分人格层、规则层、日常记忆、事务性、一过性等，使用时按层注入，而不是一股脑塞进去。

## 文档

- [`docs/design-risks-review.md`](docs/design-risks-review.md) — **动手之前的风险盘点**：这个使用模式有哪些坑、哪些"看起来想清楚了"的地方其实站在没验证过的假设上、建议先跑哪几个实验。依据是 workforce 仓库过去两周积累的真实实测记录。

## 相关仓库

- [`Grant-Huang/workforce`](https://github.com/Grant-Huang/workforce) — 语音输入客户端（网页版）。它的 `web-demo/README.md`、`docs/app-design.md`、`docs/roadmap-todo.md` 里有大量对接 Qwen-Omni-Realtime 的实测记录（记忆注入方式、限流行为、热词支持情况、`modalities` 行为等），是 omni-server 设计的主要输入，不要重新踩一遍。
