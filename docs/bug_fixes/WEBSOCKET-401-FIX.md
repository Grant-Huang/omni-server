# WebSocket 401 认证错误修复

**问题**: 部署后 APP 连接服务端实时语音会话时抛出：
```
upstream connect failed: server rejected WebSocket connection: HTTP 401
```

**根本原因**: DashScope Realtime API 要求 API Key 作为 URL 查询参数传递，但代码使用的是 Authorization HTTP Header。

**解决方案**: 已修复 `omni-server/omni/upstream.py`，将 API Key 从 HTTP Header 改为 URL 查询参数。

---

## 修改内容

### 文件: omni-server/omni/upstream.py

**变化 1**: `realtime_url()` 函数现在接收 `api_key` 参数
```python
def realtime_url(workspace_id: str, model: str, api_key: str = "") -> str:
    # ... 生成基础 URL ...
    url = f"{base}?model={model}"
    if api_key:
        url += f"&key={api_key}"  # ← 添加 API Key 作为查询参数
    return url
```

**变化 2**: `DashScopeUpstream.connect()` 不再使用 Authorization Header
```python
# 修改前:
ws = await websockets.connect(
    url, additional_headers={"Authorization": f"Bearer {api_key}"}, open_timeout=open_timeout
)

# 修改后:
url = realtime_url(sanitize_workspace_id(workspace_id), model, api_key)
ws = await websockets.connect(url, open_timeout=open_timeout)  # 无 Authorization Header
```

---

## 部署步骤

### 1. 更新后端代码
```bash
cd /home/user/omni-server
git pull origin main  # 拉取最新代码（包含此修复）
```

### 2. 确认环境变量配置

**必需**:
```bash
export QWEN_API_KEY=your_api_key_here        # DashScope API Key
export QWEN_WORKSPACE_ID=your_workspace_id   # DashScope Workspace ID
export QWEN_REALTIME_MODEL=qwen-realtime-api # 实时模型
```

**可选**:
```bash
export QWEN_TEXT_MODEL=qwen-max              # 文本模型（默认: qwen-max）
export QWEN_VOICE=Ethan                      # 语音角色（默认: Ethan）
```

### 3. 启动后端服务
```bash
cd /home/user/omni-server
python3 -m omni.server
# 或使用 .env 文件:
# 在项目根目录创建 .env 文件，包含上述环境变量
# python3 -m omni.server 会自动读取
```

### 4. 验证后端可达
```bash
curl http://localhost:8770/api/config
# 应返回 JSON，如果得到 QWEN_API_KEY 错误，检查环境变量
```

### 5. 部署前端（如需更新）

前端没有改动，但需要配置后端 URL。有两种方式：

**方式 A: 使用查询参数（临时）**
```
http://localhost:8000/talk.html?server=http://localhost:8770
```
URL 参数会被保存到 localStorage，重新加载页面后仍然生效。

**方式 B: 修改代码配置（永久）**
编辑 `app/omniServerConfig.js` 第 11 行：
```javascript
const DEFAULT_BASE = "http://localhost:8770";  // ← 改为你的后端地址
```

### 6. 前端服务（如需启动）
```bash
cd /home/user/omni/app
python3 -m http.server 8000
# 访问: http://localhost:8000
```

---

## 测试 WebSocket 连接

### 方式 1: 浏览器开发者工具
1. 打开 talk.html
2. F12 打开开发者工具 → Network 标签
3. 点击麦克风按钮开始录音
4. 查看 Network 标签中的 `ws://...` 条目：
   - 状态码应该是 **101 Switching Protocols** (连接成功)
   - 如果显示 **401**，说明认证仍然失败

### 方式 2: 后端日志
启动后端时观察日志输出：
```
# 成功:
INFO: WebSocket /ws connected from ...
INFO: DashScope upstream connected successfully

# 失败:
ERROR: upstream connect failed: ...
```

### 方式 3: 命令行测试 (需要 python-websockets)
```bash
python3 -c "
import asyncio
import websockets

async def test():
    url = 'wss://workspace-id.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=qwen-realtime-api&key=YOUR_API_KEY'
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            print('✅ WebSocket connection successful')
            await ws.close()
    except Exception as e:
        print(f'❌ WebSocket connection failed: {e}')

asyncio.run(test())
"
```

---

## 常见问题排查

### 问题 1: "QWEN_API_KEY not set"
**原因**: 环境变量未正确设置
**解决**:
```bash
# 检查环境变量
echo $QWEN_API_KEY
echo $QWEN_WORKSPACE_ID

# 如果为空，设置它们:
export QWEN_API_KEY=sk-xxx
export QWEN_WORKSPACE_ID=workspace-xxx

# 然后重新启动后端
```

### 问题 2: "invalid WebSocket URL"
**原因**: API Key 或 Workspace ID 包含特殊字符（如引号、反引号）
**解决**: upstream.py 已处理，但可以手动检查：
```bash
# 不应包含引号、反引号等:
echo $QWEN_API_KEY | grep -E '[\"`'"'"'"]'  # 如果有输出，说明包含特殊字符
```

### 问题 3: 连接卡在 "连接中..."
**原因**: 
- 后端未启动
- 防火墙阻止了 WebSocket 连接
- 后端 URL 配置错误

**解决**:
```bash
# 1. 检查后端是否运行
lsof -i :8770

# 2. 检查后端日志
tail -f /home/user/omni-server/omni.log

# 3. 测试网络连接
curl http://localhost:8770/api/config

# 4. 检查前端配置
# 浏览器 DevTools → Application → LocalStorage → omni.serverBase
```

### 问题 4: "upstream connect failed: XXX (其他错误)"
**解决步骤**:
1. 检查 QWEN_API_KEY 有效性（访问 DashScope 控制台）
2. 检查 QWEN_WORKSPACE_ID 正确性（从 DashScope 控制台复制）
3. 检查是否使用了正确的模型名称 (`qwen-realtime-api`)
4. 查看后端日志，搜索详细错误信息

---

## 完整启动脚本

```bash
#!/bin/bash
set -e

echo "=== Starting omni-server ==="

# 设置环境变量（如果需要）
export QWEN_API_KEY="${QWEN_API_KEY:-sk_xxx_change_me}"
export QWEN_WORKSPACE_ID="${QWEN_WORKSPACE_ID:-workspace-xxx-change-me}"
export QWEN_REALTIME_MODEL="qwen-realtime-api"
export QWEN_TEXT_MODEL="qwen-max"

# 启动后端
cd /home/user/omni-server
python3 -m omni.server &
BACKEND_PID=$!

echo "Backend started (PID: $BACKEND_PID)"
echo "WebSocket endpoint: ws://localhost:8770/ws"
echo "API endpoint: http://localhost:8770/api/config"

# 启动前端（可选）
cd /home/user/omni/app
python3 -m http.server 8000 &
FRONTEND_PID=$!

echo "Frontend started (PID: $FRONTEND_PID)"
echo "Open browser: http://localhost:8000/talk.html"
echo ""
echo "To stop services:"
echo "  kill $BACKEND_PID $FRONTEND_PID"
```

---

## 相关文档

- [`omni-server/omni/upstream.py`](../omni-server/omni/upstream.py) - WebSocket 连接实现
- [`omni-server/omni/server.py`](../omni-server/omni/server.py) - /ws 端点处理
- [`omni/app/omniServerConfig.js`](../omni/app/omniServerConfig.js) - 前端服务器配置
- DashScope 官方文档: https://dashscope.console.aliyun.com/

---

**提交**: f9bdfd4 - Fix WebSocket 401 auth error: use API key as query parameter
**时间**: 2026-08-30
**状态**: ✅ 已合并到 main 分支，等待部署

