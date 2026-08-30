# omni-server 文档

## 核心架构文档

- **[architecture.md](architecture.md)** - 系统架构、组件边界、数据结构
- **[design-risks-review.md](design-risks-review.md)** - 通用多用户语音后端设计风险盘点
- **[memory-design.md](memory-design.md)** - 分层记忆系统设计依据
- **[family-app-architecture.md](family-app-architecture.md)** - V2 产品架构细化规划
- **[roadmap.md](roadmap.md)** - 分阶段落地计划

## 调试与优化

- **[voice-session-tuning.md](voice-session-tuning.md)** - 语音会话的所有可调试参数（延时、缓冲、噪音处理等）

## Bug 修复与问题解决

### bug_fixes/ 目录
存放已解决的 Bug 和问题修复文档，包括根本原因分析和解决方案。

- **[bug_fixes/WEBSOCKET-401-FIX.md](bug_fixes/WEBSOCKET-401-FIX.md)** - WebSocket 401 认证错误修复
  - 问题: DashScope WebSocket 连接返回 401 Unauthorized
  - 根本原因: API Key 应作为查询参数，而非 Authorization Header
  - 解决方案: 修改 upstream.py，改为 URL 查询参数认证

## 产品定义

产品定义文档位于 `omni/` 仓库（权威版本）：
- `omni/CLAUDE.md` - 产品定义与设计原则
- `omni/docs/product-definition-v0.1.md` - 完整产品规格
- `omni/docs/brand-and-ui-design.md` - 品牌与 UI 规范

---

## 文档维护说明

### 添加新的 Bug 修复
1. 在 `docs/bug_fixes/` 目录下创建文件，命名格式: `{BUG_NAME}-FIX.md`
2. 文档应包含：
   - 问题描述与症状
   - 根本原因分析
   - 解决方案与代码变更
   - 测试方法与验证
   - 部署步骤

### 添加新的调试指南
1. 在 `docs/` 目录下创建文件，命名格式: `{FEATURE}-tuning.md` 或 `{FEATURE}-debugging.md`
2. 文档应包含：
   - 参数说明
   - 性能优化建议
   - 调试指南
   - 常见问题排查

---

**最后更新**: 2026-08-30
