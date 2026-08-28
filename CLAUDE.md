# CLAUDE.md — 产品定义原则（服务端视角，自动加载）

本仓库是 `omni` 客户端的服务端。**产品定义的权威归属在 `omni` 仓库**，不在这里重复维护——完整版见 `omni/CLAUDE.md` 和 `omni/docs/product-definition-v0.1.md` / `product-definition-v1.0-design-requirement.md`。（`omni` 是从 `workforce` 模板项目 fork 出来作为本产品客户端的仓库；`workforce` 本身是通用模板，不带产品定义，不要参考或提交到那里。）

一句话定义：一个属于全家人的 AI，通过实时语音、照片、视频和生成式 AI，让一家三代看见彼此、连接彼此，并把共同生活变成可以传承的家庭记忆——**看见 · 连接 · 传承**。

下面只列直接影响服务端设计决策的部分。

## 隐私是架构问题，不是功能问题

至少四层可见性：Private（仅本人）/ Family Shared / Selected Members / Public（主动分享到外部）。**私人对话默认不共享，AI 不因为「同一个家庭」就自动获得访问权限**——这条必须在数据模型和权限校验层面从第一天就成立，不是上层加个开关就够。

## Memory Decision Engine：不是「重要就存」

判断一段信息该不该进入长期 Memory，需要综合：Importance + Family Relevance + Emotional/Narrative Value + Future Recall Value + User Explicit Intent。产出四态：不保存 / Private Memory / Family Candidate（等待确认）/ Family Memory。

区分三个不同的东西，不要混在一张表里：
- **Feed** = 「今天值得知道什么」（现在）
- **Memory** = 「未来值得记住什么」（过去，需要用户确认或高置信度）
- **Family Context** = AI 对这个家的长期理解层（持续增长，不是静态 Profile）

## AI 推断 ≠ 事实

推断必须带置信度和确认机制，输出措辞用「看起来」「我猜」，不能断言。这条对「记忆回访」类接口尤其关键——回访引用的必须是真实存在过的记录，服务端要能对这类生成做溯源校验，不能让模型凭上下文自由发挥。

## AI 主动性分级

Level 0（用户主动）/ 1（当前对话追问）/ 2（基于当前事件建议）/ 3（跨时间跨成员主动发现 Connection）。MVP 主要服务 Level 0–2 的调用模式；Level 3 需要的长周期检索和触发机制留到后续版本，不要在 MVP 阶段就把这类接口做成默认路径。

## 渐进式 Context 补充（Value-for-Context Exchange）

对话中向用户请求补充信息时，必须让用户能立即理解「告诉你之后我能帮你做什么」，服务端提示词/接口设计要支持这种「先说明用途、再请求」的顺序，不能做成无差别的资料收集接口。

## 连接类接口：只生成建议，不代为执行

任何「帮 A 联系 B」的能力，服务端只应产出建议话术，交由本人确认后再触发实际发送——不要在服务端提供「AI 直接代发消息」这类捷径接口，这会直接违反「连接而非替代」这条产品原则。

## 完整参考

- `omni/CLAUDE.md`（产品定义权威版本）
- `omni/docs/product-definition-v0.1.md`
- `omni/docs/product-definition-v1.0-design-requirement.md`
- [`docs/design-risks-review.md`](docs/design-risks-review.md)（本仓库现有的服务端设计风险盘点）
