# WebSocket 401 错误 — 根因分析

## 问题重述

部署后，omni.server 连接 DashScope Realtime API 时出现 HTTP 401 错误。

## 错误的"修复"

之前的修复方案是将 API 密钥从 Authorization HTTP header 改为 URL query parameter：
```python
# "修复"后的代码
url = f"{base}?model={model}&key={api_key}"
ws = await websockets.connect(url, open_timeout=10_000)
```

这个方案声称 DashScope "requires" query parameter，但**实际测试表明这是错误的**。

## 真实诊断结果

部署团队的独立脚本测试揭示了关键信息：

| 方法 | 独立脚本 | omni.server 进程 |
|---|---|---|
| Authorization header | ✅ OK | ❌ 401 |
| URL query parameter | ❌ 401 | ❌ 401 |

**结论**：
- Authorization header 方式**本身是正确的**（独立脚本验证）
- 问题不在 API 密钥传递方式，而在 **omni.server 进程的 context**
- 单纯改 URL 参数是表面修复，掩盖了真正问题

## 真正的问题可能原因

### 1. 环境变量配置
```python
# Config.from_env() 是否正确读取 QWEN_API_KEY？
cfg = Config.from_env()
print(f"api_key length: {len(cfg.api_key) if cfg.api_key else 0}")
print(f"workspace_id: {cfg.workspace_id or 'NOT SET'}")
```

### 2. 网络环境/代理差异
- 独立脚本运行环境 vs omni.server 部署环境的网络差异
- HTTPS_PROXY 设置（系统备注提到 `/root/.ccr/ca-bundle.crt`）
- TLS 证书验证

### 3. 依赖版本
- websockets 库版本差异
- 其他网络中间件

### 4. 进程上下文
- omni.server 的中间件或连接池配置
- aiohttp 与 websockets 的交互

## 当前修复方案

已恢复到 Authorization header 方式，但**需要逐步诊断真正原因**：

```python
# 当前代码（正确的方式）
headers = {}
if api_key:
    headers["Authorization"] = f"Bearer {api_key}"
ws = await websockets.connect(url, additional_headers=headers, open_timeout=10_000)
```

## 诊断步骤

### 第 1 步：运行诊断脚本
```bash
cd /home/user/omni-server
python3 diagnose_websocket.py
```

这个脚本会在 omni.server context 中测试两种方式，输出详细日志。

**预期结果**：
- Authorization header 方式应该恢复工作
- 如果仍然 401，可能指向环境变量或网络配置问题

### 第 2 步：验证环境配置
如果诊断脚本仍然失败，检查：

```bash
# 检查环境变量是否设置
echo "QWEN_API_KEY: ${QWEN_API_KEY:0:10}..."
echo "QWEN_WORKSPACE_ID: $QWEN_WORKSPACE_ID"
echo "QWEN_REALTIME_MODEL: $QWEN_REALTIME_MODEL"
echo "HTTPS_PROXY: $HTTPS_PROXY"
```

### 第 3 步：检查网络连接
```bash
# 测试 DNS 和基本连接
curl -v wss://dashscope.aliyuncs.com/api-ws/v1/realtime 2>&1 | head -20

# 如果有代理，检查代理状态
curl -sS "$HTTPS_PROXY/__agentproxy/status" 2>&1 | head -10
```

### 第 4 步：独立脚本对比
如果 omni.server context 中仍然失败，但诊断脚本失败，可能是中间件干扰。

## 下一步行动

1. **立即**：运行 `diagnose_websocket.py`，记录输出
2. **如果 Authorization header 恢复**：部署更新，监测生产环境
3. **如果仍然 401**：
   - 检查环境变量配置
   - 检查网络/代理设置
   - 考虑 TLS 证书验证问题
   - 检查 websockets 库版本兼容性

## 完整历史

- **第一次修复（错误）**：改为 URL query parameter，隐盖了真正问题
- **部署团队反馈**：Authorization header 在独立脚本中有效
- **第二次修复（当前）**：恢复 Authorization header，准备深度诊断
