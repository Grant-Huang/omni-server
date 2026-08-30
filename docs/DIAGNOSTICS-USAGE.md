# 服务端诊断日志使用指南

**无需浏览器 console**，所有音频流向事件直接输出到服务器日志文件。

## 快速开始

```bash
# 在 omni-server 目录下运行
cd /path/to/omni-server

# 实时监测日志
tail -f logs/audio-diagnostics.log
```

## 日志位置

```
omni-server/
├── logs/
│   └── audio-diagnostics.log  ← 所有诊断信息输出到这里
```

**首次运行时，`logs/` 目录会自动创建。**

---

## 客户端与服务端日志对照（关键技巧）

### 通过 Session ID 关联日志

客户端和服务端都记录同一个 `sessionId`，可以用来完整追踪一个对话的整个生命周期。

**客户端**（浏览器 Console）：
```
[SERVER EVENT] session.created { sessionId: "abc123def456", ... }
[SERVER EVENT] response.audio.delta { sessionId: "abc123def456", delta: "..." }
[AUDIO DELTA] { sessionId: "abc123def456", bytes: 1024 }
```

**服务端**（logs/audio-diagnostics.log）：
```
[SESSION] connected session=abc123def456 user_scope=user:test
[UPSTREAM] response.audio.delta session=abc123def456 size=1024B
[CLIENT] response.audio.delta session=abc123def456 size=1024B status=OK
```

### 对照方法

1. **打开浏览器 DevTools** (F12) → Console 标签页
2. **运行一次完整对话**（说话 → 等待回复）
3. **记下 `sessionId`**（从 `[SERVER EVENT]` 日志中获取）
4. **在服务端日志中搜索该 ID**：
   ```bash
   grep "session=abc123def456" logs/audio-diagnostics.log
   ```

### 完整对比示例

假设客户端显示：
```
[SERVER EVENT] session.created { sessionId: "xyz789", ... }
[SERVER EVENT] response.audio.delta { sessionId: "xyz789", delta: "base64data" }
[AUDIO DELTA] { sessionId: "xyz789", bytes: 2048 }
```

在服务端日志中搜索 `session=xyz789`：
- ✅ 如果找到 `[CLIENT] response.audio.delta session=xyz789 size=2048B status=OK`
  → 完整流程工作正常
- ❌ 如果只找到 `[UPSTREAM] response.audio.delta session=xyz789`，但没有 `[CLIENT]`
  → 问题在转发层（`to_client` 失败）
- ❌ 如果完全找不到
  → 问题在上游或连接层

---

## 日志格式

```
2026-08-30 14:23:45 | INFO     | omni.diagnostics | [SESSION] connected session=session_1693214625_abc123 user_scope=user:test
2026-08-30 14:23:46 | INFO     | omni.diagnostics | [UPSTREAM] session.created size=245B session=session_1693214625_abc123
2026-08-30 14:23:46 | INFO     | omni.diagnostics | [CLIENT] session.created session=session_1693214625_abc123 size=245B status=OK
2026-08-30 14:23:50 | INFO     | omni.diagnostics | [UPSTREAM] conversation.item.input_audio_transcription.completed size=156B session=session_1693214625_abc123
2026-08-30 14:23:51 | INFO     | omni.diagnostics | [UPSTREAM] response.audio.delta size=1024B session=session_1693214625_abc123
2026-08-30 14:23:51 | INFO     | omni.diagnostics | [CLIENT] response.audio.delta session=session_1693214625_abc123 size=1024B status=OK
```

**列解释**：
- `timestamp` — 事件发生的精确时间
- `LEVEL` — 日志级别（INFO/ERROR/DEBUG）
- `omni.diagnostics` — 记录器名称
- `[SESSION/UPSTREAM/CLIENT]` — 事件类型标记
- 详情 — 具体事件、session ID、数据大小

---

## 诊断步骤

### 问题：收不到音频

**预期的日志序列**：
```
[SESSION] connected session=XXX
[SESSION] upstream_connected session=XXX model=qwen3.5-omni-realtime
[UPSTREAM] conversation.item.input_audio_transcription.completed session=XXX  ← 用户说话被识别
[UPSTREAM] response.created session=XXX                                     ← 服务器开始生成回复
[UPSTREAM] response.audio.delta session=XXX size=1024B                      ← 音频数据到达
[CLIENT] response.audio.delta session=XXX size=1024B status=OK              ← 转发给客户端
```

**逐步检查**：

#### 第 1 步：连接是否建立？
```bash
grep "connected" logs/audio-diagnostics.log | head -1
```
**预期**：看到 `[SESSION] connected`  
**如果没有**：客户端连接失败，检查网络/防火墙

---

#### 第 2 步：DashScope 上游是否连接？
```bash
grep "upstream_connected" logs/audio-diagnostics.log | head -1
```
**预期**：看到 `[SESSION] upstream_connected session=XXX model=qwen3.5-omni-realtime`  
**如果没有**：DashScope 连接失败
- 检查 `QWEN_API_KEY` 是否设置正确
- 检查 `QWEN_WORKSPACE_ID` 是否设置正确
- 检查网络是否能访问 `cn-beijing.maas.aliyuncs.com`

---

#### 第 3 步：客户端有没有说话？
```bash
grep "input_audio_transcription.completed" logs/audio-diagnostics.log
```
**预期**：看到 `[UPSTREAM] conversation.item.input_audio_transcription.completed`  
**如果没有**：
- 用户麦克风被录音，但 DashScope 的 ASR 没有返回转录
- 可能是音频格式问题或网络超时

---

#### 第 4 步：服务器有没有开始生成回复？
```bash
grep "response.created" logs/audio-diagnostics.log
```
**预期**：看到 `[UPSTREAM] response.created`  
**如果没有**：
- DashScope 没有开始生成文本回复
- 检查 API 配额或服务状态

---

#### 第 5 步：DashScope 有没有发送音频？⭐ **关键**
```bash
grep "response.audio.delta" logs/audio-diagnostics.log
```
**预期**：看到多条 `[UPSTREAM] response.audio.delta size=...B`  
**如果没有**：
- ❌ **问题在上游（DashScope）**：模型可能没有输出音频
- 检查 DashScope 的 output_audio_format 配置
- 确认使用的是 qwen3.5-omni-realtime（支持音频输出的模型）

---

#### 第 6 步：服务器有没有转发音频给客户端？
```bash
grep "\[CLIENT\] response.audio.delta" logs/audio-diagnostics.log
```
**预期**：看到 `[CLIENT] response.audio.delta session=XXX size=...B status=OK`  
**如果没有**：
- ❌ **问题在转发层**：omni-server 的 to_client 回调失败
- 检查是否有 `status=FAIL` 的日志条目
- 查看错误日志（搜索 `ERROR`）

---

### 问题：连接断开

```bash
grep "closed\|ERROR" logs/audio-diagnostics.log
```

**常见错误**：
```
ERROR: upstream connect failed: ...
ERROR: pump_upstream: socket timeout
ERROR: _notify_client: client connection closed
```

---

## 完整诊断模板

复制这个命令快速诊断：

```bash
#!/bin/bash
echo "=== Connection Status ==="
tail -1 logs/audio-diagnostics.log | grep connected

echo -e "\n=== Upstream Connection ==="
tail -20 logs/audio-diagnostics.log | grep upstream_connected

echo -e "\n=== User Speech Recognition ==="
tail -50 logs/audio-diagnostics.log | grep input_audio_transcription.completed

echo -e "\n=== Audio Stream (DashScope → omni-server) ==="
grep "response.audio.delta" logs/audio-diagnostics.log | wc -l
echo "Total audio chunks received from DashScope:"
grep "\[UPSTREAM\] response.audio.delta" logs/audio-diagnostics.log | head -3

echo -e "\n=== Client Forward (omni-server → browser) ==="
grep "\[CLIENT\] response.audio.delta" logs/audio-diagnostics.log | head -3

echo -e "\n=== Errors ==="
grep "ERROR" logs/audio-diagnostics.log || echo "No errors"
```

---

## 日志rotation和管理

日志文件每次服务重启时清空。如需保存历史日志：

```bash
# 归档当前日志
cp logs/audio-diagnostics.log logs/audio-diagnostics.$(date +%Y%m%d_%H%M%S).log

# 清空当前日志
> logs/audio-diagnostics.log
```

---

## 在生产环境中

**建议**：配置日志收集系统（如 ELK Stack、Datadog）来聚合这些日志。

如需禁用日志输出（性能考虑），可以在 `diagnostics.py` 中注释掉 `console_handler`：

```python
# diagnostics_logger.addHandler(console_handler)  # 注释掉这行
```

但**强烈建议保留文件日志**——调试成本远低于运行性能的微小影响。

---

## 快速参考

| 目标 | 命令 |
|---|---|
| 实时监测 | `tail -f logs/audio-diagnostics.log` |
| 看最后 10 行 | `tail logs/audio-diagnostics.log` |
| 搜索 session ID | `grep "session_1693214625_abc123" logs/audio-diagnostics.log` |
| 统计音频块 | `grep "\[UPSTREAM\] response.audio.delta" logs/audio-diagnostics.log \| wc -l` |
| 看所有错误 | `grep ERROR logs/audio-diagnostics.log` |
| 清空日志 | `> logs/audio-diagnostics.log` |
