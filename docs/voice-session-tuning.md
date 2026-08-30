# 语音会话调试参数配置

**概述**: 本文档记录了语音会话的所有可调试参数，包括客户端和服务端的延时、缓冲、噪音处理等配置。这些参数基于在真实设备上的测量值（来自 workforce 项目的音频管线实测数据）。

---

## 客户端参数 (omni/app/talk.js)

### 麦克风约束
```javascript
const MIC_CONSTRAINTS = {
  audio: {
    echoCancellation: true,      // 回声消除（减少讲话者自己的声音反馈）
    noiseSuppression: true,      // 噪声抑制（降低背景噪声）
    autoGainControl: true        // 自动增益控制（自动调节音量）
  }
};
```

### 超时控制
```javascript
const IDLE_TIMEOUT_MS = 5 * 60 * 1000;  // 5分钟：无用户输入时自动挂断
const CONNECT_TIMEOUT_MS = 8000;        // 8秒：WebSocket 连接超时
const ACK_TIMEOUT_MS = 4000;            // 4秒：session.update 确认超时
```

**参数说明**:
- `IDLE_TIMEOUT_MS`: 用户在 LISTENING 状态下超过 5 分钟没有说话，自动终止会话。这防止了僵尸连接。
- `CONNECT_TIMEOUT_MS`: WebSocket 打开后，8 秒内必须收到 `session.created` 事件，否则视为连接失败。
- `ACK_TIMEOUT_MS`: 发送 `session.update` 后，4 秒内必须收到 `session.updated` 确认。超时后继续执行（不阻塞）。

### 语音活动检测 (VAD)
```javascript
const VAD = {
  threshold: 0.6,      // 0-1 范围：检测语音的音量阈值（越低越敏感）
  silenceMs: 900       // 毫秒：检测到此时长沉默后，认为用户停止说话
};
```

**参数说明**:
- `threshold`: 0.6 意味着音量需要达到平均频谱的 60% 才被认为是有效的语音。较低的值会更敏感但可能引入噪声。
- `silenceMs`: 900 ms（0.9 秒）的沉默后认为用户说完了。这是一个平衡值，既不会过早中断，也不会延迟过长。

### 播放控制
```javascript
const PLAYBACK = {
  fadeMs: 15,          // 毫秒：音频淡出时间（平滑过渡）
  prebufferMs: 150     // 毫秒：预缓冲时间（减少播放延迟）
};
```

**参数说明**:
- `fadeMs`: 15 ms 的淡出时间用于平滑停止音频播放，避免突然的弹音。
- `prebufferMs`: 150 ms 的预缓冲确保音频播放不会因网络抖动而卡顿。

### 打断确认
```javascript
const BARGE_IN_CONFIRM_MS = 250;        // 毫秒：打断确认持续时间
const BARGE_IN_CONFIRM_LEVEL = 0.12;    // 0-1 范围：打断确认音量级别
```

**参数说明**:
- 当用户在 AI 说话时（SPEAKING 状态）插入新的话语时，系统会检测是否是真实的打断。
- `BARGE_IN_CONFIRM_MS`: 在 250 ms 窗口内持续采样音量。
- `BARGE_IN_CONFIRM_LEVEL`: 平均音量必须超过 12% 才被认为是真实的打断，而不是 AI 语音的回声。
- 这个机制防止了 AI 自己的语音触发打断。

### 音频编码
```javascript
// 客户端将音频转换为 PCM16（16-bit 单声道）
// 采样率: 16kHz（由 downsampleTo16k 函数处理）
// 格式: Base64 编码传输
```

---

## 服务端参数 (omni-server/omni/realtime.py)

### 响应合并控制
```python
SELF_INTERRUPT_WINDOW_S = 2.5  # 秒：自中断窗口
```

**参数说明**:
- AI 开始回复后，如果在 2.5 秒内从 Sidecar（内存查询）接收到新的结果，系统会尝试自中断当前响应并注入新信息。
- 超过 2.5 秒，AI 已经说了足够多的内容，中途打断会显得不自然，所以改为排队等待当前响应完成后再追加。

### 确认超时
```python
ACK_TIMEOUT_S = 4.0  # 秒：session.update 确认超时
```

**参数说明**:
- 向 DashScope 发送 `session.update` 后，最长等待 4 秒钟收到 `session.updated` 确认。
- 这是从 workforce 项目的实测中获取的值，在工作区（workspace）域通常能更快地收到确认。
- 超时后继续执行（不阻塞对话），因为单次确认失败不应该挂起整个会话。

### 音频传输
```python
# WebSocket 消息大小限制
max_msg_size = 10 * 1024 * 1024  # 10 MB：单个 WebSocket 消息最大尺寸
```

---

## 性能优化建议

### 网络延迟高的环境
```javascript
// 增加超时时间
const CONNECT_TIMEOUT_MS = 15000;  // 从 8 秒增加到 15 秒
const ACK_TIMEOUT_MS = 8000;       // 从 4 秒增加到 8 秒
```

### 嘈杂环境
```javascript
// 调整 VAD 参数
const VAD = {
  threshold: 0.5,      // 从 0.6 降低到 0.5（更敏感）
  silenceMs: 1200      // 从 900 增加到 1200（更宽容）
};
```

### 低延迟场景
```javascript
const PLAYBACK = {
  fadeMs: 5,           // 从 15 ms 减少到 5 ms
  prebufferMs: 50      // 从 150 ms 减少到 50 ms
};
```

### 打断敏感度调整
```javascript
// 提高打断敏感度（更容易打断 AI）
const BARGE_IN_CONFIRM_MS = 150;     // 从 250 减少到 150
const BARGE_IN_CONFIRM_LEVEL = 0.08; // 从 0.12 降低到 0.08

// 降低打断敏感度（防止误触发）
const BARGE_IN_CONFIRM_MS = 400;     // 从 250 增加到 400
const BARGE_IN_CONFIRM_LEVEL = 0.15; // 从 0.12 增加到 0.15
```

---

## 调试指南

### 1. 测试连接延迟
```javascript
// 在 talk.js 中添加时间戳记录
console.time("ws-connect");
ws = new WebSocket(OmniServer.wsUrl("/ws"));
ws.onopen = () => {
  console.timeEnd("ws-connect");  // 输出连接耗时
};
```

### 2. 监测 VAD 触发
```javascript
// 在 processorNode 中添加日志
processorNode.port.onmessage = (event) => {
  const rms = calculateRMS(event.data);  // 计算音量
  if (rms > VAD.threshold) {
    console.log("Speech detected:", rms);
  }
};
```

### 3. 检查打断确认
浏览器开发者工具 → Console，搜索 "barge-in" 相关日志。

### 4. 服务端响应合并日志
后端日志中搜索 "SELF_INTERRUPT" 或 "merge decision"，观察何时触发自中断。

---

## 参考资源

- **客户端完整配置**: `omni/app/talk.js` (第 28-41 行)
- **服务端完整配置**: `omni-server/omni/realtime.py` (第 51-57 行)
- **音频管线**: `omni/app/talk.js` (downsampleTo16k, floatTo16BitPCM 等函数)
- **Workforce 原始测量**: docs/workforce/app-design.md
- **DashScope 文档**: https://dashscope.console.aliyun.com/

---

## 历史变更

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2026-08-30 | 初始配置文档，从 talk.js 和 realtime.py 提取参数 |

