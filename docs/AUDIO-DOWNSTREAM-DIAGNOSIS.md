# 音频下行诊断指南

**现象**：客户端可以说话（上行✅），服务器能接收（心跳✅），但收不到回话语音（下行❌）

## 根本差异：Workforce vs omni 架构

### Workforce（单体 = 字节泵）
```
Browser ←→ Workforce(单进程) ←→ DashScope
          同一进程内的内存转发，帧不会丢
```

### omni（分离 = 事件解析 + 转发）
```
Browser ←→ omni-server(独立进程/容器) ←→ DashScope
          跨网络 WebSocket，转发层可能失败
```

**omni-server 不能是字节泵**，因为必须：
- 看到转录文本来检索记忆
- 在中间注入查询结果
- 处理"说话中途有查询结果"的合流逻辑

**代价**：解析意味着转发环节多了一层，问题面更大。

---

## 诊断步骤（按顺序）

### 第1步：浏览器开发者工具（立即执行）

1. 打开浏览器开发者工具（F12 或 右键 → 检查）
2. 切换到 **Console** 标签页
3. 说话并等待服务器回复
4. **看有没有 `[SERVER EVENT]` 日志**

**预期看到**：
```
[SERVER EVENT] session.created {...}
[SERVER EVENT] conversation.item.input_audio_transcription.completed {transcript:"..."}
[SERVER EVENT] response.created {...}
[SERVER EVENT] response.audio.delta {delta: "..."} ← 关键！
[SERVER EVENT] response.audio.delta {delta: "..."}
...
[SERVER EVENT] response.done {...}
```

**如果看不到 `response.audio.delta`**：问题在**上游（DashScope → omni-server）**
**如果能看到但没声音**：问题在**音频播放端（omni 客户端）**

---

### 第2步：确认 DashScope 真的在发送音频

**假设**：`response.audio.delta` 事件根本没到达 omni-server

在 omni-server 的 `server.py` 中加临时日志：

```python
# 在 pump_upstream() 函数中加（第 286 行后）：
async def pump_upstream() -> None:
    while True:
        event = await upstream.recv()
        if event.get("type") == "response.audio.delta":
            log.info(f"[AUDIO] received delta, {len(event.get('delta', ''))} bytes")
        _record(transcript, event)
        await session.handle_upstream_event(event)
```

重启 omni-server，再试一次说话。
- **看到 `[AUDIO] received delta` 日志**：上游工作，问题在转发
- **看不到**：DashScope 没发音频或连接断了

---

### 第3步：检查事件是否被正确转发

在 `realtime.py` 的 `handle_upstream_event` 中加日志（第 282 行）：

```python
async def handle_upstream_event(self, event: dict) -> None:
    etype = event.get("type")
    
    # 诊断
    if etype == "response.audio.delta":
        log.info(f"[REALTIME] forwarding audio delta to client, {len(event.get('delta', ''))} bytes")
    
    if etype not in _INTERCEPTED:
        await self._notify_client(event)  # pass through untouched
        return
    # ...
```

**日志位置**：
- 如果出现 `[AUDIO] received delta` 但没有 `[REALTIME] forwarding audio delta`：问题在 handle_upstream_event 的逻辑
- 如果两个都有但浏览器 Console 没看到：问题在 to_client 转发

---

### 第4步：网络诊断（如果前面都没问题）

如果日志都有，但浏览器没收到，可能是：

**1. 连接本身的问题**
```bash
# 检查 omni-server 到 DashScope 的连接状态
netstat -an | grep -i 'cn-beijing\|dashscope'
# 或者用 tcpdump（需要权限）
tcpdump -i any -A 'tcp port 443' | grep -i 'audio\|delta'
```

**2. Cloudflare 或代理的 WebSocket 限制**
```bash
# 测试 WebSocket 是否通畅
curl -i -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: xxx" \
  -H "Sec-WebSocket-Version: 13" \
  https://your-omni-server.com/ws
```

**3. 浏览器 CORS 或 Content-Security-Policy**
- 在开发者工具 → Network 标签，找 WebSocket 连接
- 查看 Response Headers 是否有 CORS 错误

---

### 第5步：客户端音频播放问题

**假设**：事件到了，但 `playPCM16Chunk()` 失败

在 `talk.js` 的 `playPCM16Chunk` 函数中加日志：

```javascript
function playPCM16Chunk(base64Data) {
  if (!base64Data) {
    console.warn("[PLAYBACK] empty audio chunk");
    return;
  }
  try {
    const pcm16 = base64ToInt16(base64Data);
    console.log("[PLAYBACK]", "queuing", pcm16.length, "samples");
    // 原有代码...
  } catch (e) {
    console.error("[PLAYBACK] error:", e);
  }
}
```

**常见问题**：
- 音频上下文（AudioContext）未初始化
- playback worklet 加载失败
- 浏览器的音频输出权限被拒

---

## 快速排查流程图

```
客户端说话？
  ├─ 否 → 麦克风权限问题（另外诊断）
  └─ 是 → 浏览器 Console 有 [SERVER EVENT] 吗？
         ├─ 没有任何事件 → WebSocket 连接可能断了
         ├─ 有 session.created/transcription.completed → 
         │  omni-server 没收到 DashScope 音频 → 检查 DashScope 连接/API Key
         ├─ 有 response.audio.delta → 
         │  └─ 浏览器 Console 有 [AUDIO DELTA] 日志吗？
         │     ├─ 没有 → 客户端 handleServerEvent 没收到（网络问题）
         │     └─ 有 → playPCM16Chunk 失败（音频系统问题）
```

---

## 关键对比点：Workforce vs 现在

| 环节 | Workforce | omni |
|---|---|---|
| 浏览器 ← 事件 | WebSocket | WebSocket |
| 事件处理 | 简单转发（字节泵） | 解析 + 逻辑处理 + 转发 |
| 可能失败的地方 | WS 本身、DashScope | WS、DashScope、VoiceSession 解析、to_client |

现在比 Workforce 多了**两个可能失败的地方**：事件解析和转发。

---

## 立即可以做的

1. ✅ 已加日志：`talk.js` 的 Console 日志
2. 需要加日志：
   - `server.py` 的 pump_upstream (诊断上游接收)
   - `realtime.py` 的 handle_upstream_event (诊断转发)
   - `talk.js` 的 playPCM16Chunk (诊断播放)

**最快的方法**：打开浏览器 Console，看第 1 步的日志，告诉我看到了什么。
