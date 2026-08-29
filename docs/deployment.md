# omni-server 部署指南

本文档介绍如何部署「家里」服务端，包括实时语音 AI 后端和静态营销站点。

## 架构概述

omni-server 包含**两个独立的部署单元**，部署策略不同：

| 单元 | 类型 | 部署方式 | 技术栈 |
|------|------|---------|--------|
| **Python 后端** (`omni/server.py`) | 常驻进程 | 云主机/容器 | Python 3 + aiohttp/websockets |
| **营销站点** (`marketing-site/`) | 静态内容 | 静态托管 | HTML/CSS（Netlify/Vercel/GitHub Pages） |

## 部分 1: Python 后端部署

### 前置要求

- Python 3.8+
- Qwen API Key（用于 LLM 调用）
- Qwen Workspace ID（建议配置，避免 WebSocket 静默丢包）
- 网络访问权限（WebSocket 用于实时语音）

### 本地开发与测试

```bash
# 安装依赖
pip install aiohttp websockets python-dotenv

# 创建 .env 文件
cat > .env << EOF
QWEN_API_KEY=sk-xxxxxxxxxxxx
QWEN_WORKSPACE_ID=llm-xxxxxxxxxxxx
EOF

# 启动开发服务器（本地 8770 端口）
QWEN_API_KEY=sk-xxxxxxxxxxxx QWEN_WORKSPACE_ID=llm-xxxxxxxxxxxx python3 -m omni.server

# 或使用 .env 文件
python3 -m omni.server
```

### 测试（离线，无配额消耗）

```bash
# 运行所有单元测试
python3 -m unittest discover -s tests -p "test_*.py"

# 或运行特定测试文件
python3 -m unittest tests.test_memory
python3 -m unittest tests.test_realtime
```

### 部署方式选择

#### 方式 1: Docker 容器（推荐）

**优势**：环境隔离、易于扩展、云平台友好

创建 `Dockerfile`：

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源代码
COPY omni ./omni
COPY tests ./tests

# 暴露端口
EXPOSE 8770

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8770/health')"

# 启动服务
CMD ["python3", "-m", "omni.server"]
```

创建 `requirements.txt`：

```
aiohttp==3.9.0
websockets==12.0
python-dotenv==1.0.0
```

构建与运行：

```bash
# 构建镜像
docker build -t omni-server:latest .

# 本地运行（测试）
docker run -it --rm \
  -e QWEN_API_KEY=$QWEN_API_KEY \
  -e QWEN_WORKSPACE_ID=$QWEN_WORKSPACE_ID \
  -p 8770:8770 \
  omni-server:latest

# 后台运行
docker run -d \
  --name omni-server \
  -e QWEN_API_KEY=$QWEN_API_KEY \
  -e QWEN_WORKSPACE_ID=$QWEN_WORKSPACE_ID \
  -p 8770:8770 \
  -v omni-data:/app/data \
  omni-server:latest
```

#### 方式 2: 云平台一键部署

##### Railway

```bash
# 连接 GitHub 仓库
# 1. 访问 https://railway.app/new
# 2. 选择 "Deploy from GitHub repo"
# 3. 关联本仓库
# 4. 在 Railway 环境变量中配置：
#    QWEN_API_KEY=sk-xxxxx
#    QWEN_WORKSPACE_ID=llm-xxxxx
# 5. 自动部署，获得公网地址（如 omni-server-production.up.railway.app）
```

##### Replit

```bash
# 1. 访问 https://replit.com
# 2. 导入 GitHub 仓库
# 3. 创建 .replit 文件
cat > .replit << EOF
run = "QWEN_API_KEY=$QWEN_API_KEY QWEN_WORKSPACE_ID=$QWEN_WORKSPACE_ID python3 -m omni.server"
EOF

# 4. 在 Replit Secrets 中配置环境变量
# 5. 点击运行
```

#### 方式 3: 虚拟主机（VPS/自建服务器）

```bash
# 在服务器上克隆仓库
git clone https://github.com/Grant-Huang/omni-server.git
cd omni-server

# 安装依赖
pip3 install -r requirements.txt

# 创建 systemd 服务（Linux）
sudo tee /etc/systemd/system/omni-server.service << EOF
[Unit]
Description=Omni AI Server
After=network.target

[Service]
Type=simple
User=omni
WorkingDirectory=/home/omni/omni-server
Environment="QWEN_API_KEY=sk-xxxxx"
Environment="QWEN_WORKSPACE_ID=llm-xxxxx"
ExecStart=/usr/bin/python3 -m omni.server
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 启用和启动服务
sudo systemctl daemon-reload
sudo systemctl enable omni-server
sudo systemctl start omni-server

# 查看日志
journalctl -u omni-server -f
```

#### 方式 4: 使用进程管理器（PM2/Supervisor）

```bash
# 使用 PM2（Node.js 用户）
npm install -g pm2
pm2 start "python3 -m omni.server" --name omni-server \
  --env QWEN_API_KEY=$QWEN_API_KEY \
  --env QWEN_WORKSPACE_ID=$QWEN_WORKSPACE_ID

# 保存配置
pm2 save
pm2 startup
```

### 数据持久化

默认使用 SQLite，数据文件位置：

```bash
# 本地：当前目录下的 omni_memory.db
# Docker 容器：需要挂载 volume
# 云平台：配置持久化存储
```

**配置 Docker 持久化：**

```bash
# 创建 volume
docker volume create omni-data

# 运行时挂载
docker run -d \
  --name omni-server \
  -v omni-data:/app/data \
  omni-server:latest
```

**配置完整的 docker-compose.yml：**

```yaml
version: '3.8'

services:
  omni-server:
    build: .
    container_name: omni-server
    ports:
      - "8770:8770"
    environment:
      QWEN_API_KEY: ${QWEN_API_KEY}
      QWEN_WORKSPACE_ID: ${QWEN_WORKSPACE_ID}
    volumes:
      - omni-data:/app/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8770/health"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  omni-data:
```

启动：

```bash
# 创建 .env 文件
cp .env.example .env
# 编辑 .env，填入 API Key

# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f omni-server
```

### 配置与环境变量

关键环境变量：

| 变量名 | 必需 | 说明 | 示例 |
|--------|------|------|------|
| `QWEN_API_KEY` | 是 | Qwen 大模型 API Key | `sk-xxxxx...` |
| `QWEN_WORKSPACE_ID` | 建议 | 工作区 ID，避免 WebSocket 丢包 | `llm-xxxxx...` |
| `SERVER_HOST` | 否 | 监听地址，默认 `0.0.0.0` | `127.0.0.1` |
| `SERVER_PORT` | 否 | 监听端口，默认 `8770` | `8770` |
| `DATABASE_PATH` | 否 | SQLite 数据库路径 | `/data/memory.db` |

### 反向代理配置

生产环境通常需要 Nginx 反向代理：

```nginx
upstream omni_backend {
    server 127.0.0.1:8770;
}

server {
    listen 80;
    server_name api.jia.family;

    # 重定向到 HTTPS
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name api.jia.family;

    # SSL 证书
    ssl_certificate /etc/letsencrypt/live/api.jia.family/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.jia.family/privkey.pem;

    # 安全头
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;

    # API 代理
    location / {
        proxy_pass http://omni_backend;
        proxy_http_version 1.1;
        
        # WebSocket 支持
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # 其他必要头
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # 超时配置（WebSocket 长连接）
        proxy_connect_timeout 60s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;
    }
}
```

配置 SSL 证书：

```bash
# 使用 Certbot
sudo certbot certonly -d api.jia.family
```

## 部分 2: 营销站点部署

### 本地预览

```bash
cd marketing-site
python3 -m http.server 8000
# 浏览器打开 http://localhost:8000
```

### 部署方式

#### 方式 1: Netlify（推荐）

```bash
# 安装 Netlify CLI
npm install -g netlify-cli

# 部署
cd marketing-site
netlify deploy --prod
```

或在 Netlify 网页界面：

1. 连接 GitHub 仓库
2. 配置发布目录为 `marketing-site`
3. 自动部署

#### 方式 2: Vercel

```bash
npm install -g vercel
cd marketing-site
vercel --prod
```

#### 方式 3: GitHub Pages

在仓库 Settings → Pages 中：

- 选择 Deploy from a branch
- 选择分支和 `/marketing-site` 目录

#### 方式 4: 自建服务器

```bash
# 复制文件到 Web 根目录
sudo cp -r marketing-site/* /var/www/html/

# 配置 Nginx
cat > /etc/nginx/sites-available/omni-marketing << EOF
server {
    listen 80;
    server_name jia.family www.jia.family;
    return 301 https://\$server_name\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name jia.family www.jia.family;
    
    ssl_certificate /etc/letsencrypt/live/jia.family/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/jia.family/privkey.pem;
    
    root /var/www/jia-family;
    index index.html;
    
    location / {
        try_files \$uri \$uri/ /index.html;
    }
}
EOF

# 启用站点
sudo ln -s /etc/nginx/sites-available/omni-marketing /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 集成与端点配置

### 客户端与服务端的连接

在 `omni` 客户端的 `app/omniServerConfig.js` 中配置：

```javascript
export const omniServerConfig = {
    wsUrl: 'https://api.jia.family:8770',  // 生产服务端 WebSocket 地址
};
```

或使用 URL 参数覆盖：

```
https://app.jia.family/talk.html?server=https://api.jia.family:8770
```

### CORS 配置

`omni/server.py` 已默认配置 CORS 允许跨域请求。如需调整：

```python
# omni/cors.py
ALLOWED_ORIGINS = [
    'https://app.jia.family',
    'https://app.staging.jia.family',
]
```

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/ws` | WebSocket | 实时语音会话 WebSocket 连接 |
| `/api/config` | GET | 获取客户端配置（API 功能）|
| `/health` | GET | 健康检查（服务状态） |

## 监控与日志

### 日志收集

```bash
# 本地开发：输出到控制台
python3 -m omni.server

# 生产环境：保存到文件
python3 -m omni.server 2>&1 | tee -a server.log

# 使用日志轮转（Logrotate）
cat > /etc/logrotate.d/omni-server << EOF
/var/log/omni-server.log {
    daily
    rotate 7
    compress
    delaycompress
    notifempty
    create 0640 omni omni
    sharedscripts
    postrotate
        systemctl reload omni-server > /dev/null 2>&1
    endscript
}
EOF
```

### 监控指标

关键指标：

- **服务可用性**：`/health` 端点是否返回 200
- **WebSocket 连接数**：并发会话数
- **内存使用**：SQLite 数据库大小和内存占用
- **API 延迟**：LLM 响应时间
- **错误率**：4xx/5xx 错误比例

### 使用 Prometheus + Grafana 监控（高级）

```bash
# 在 server.py 中添加 Prometheus 导出
# pip install prometheus-client

from prometheus_client import Counter, Histogram, generate_latest, REGISTRY

# 定义指标
websocket_connections = Counter('omni_websocket_connections_total', 'Total WebSocket connections')
api_duration = Histogram('omni_api_duration_seconds', 'API request duration')

# 暴露指标端点
@app.get('/metrics')
async def metrics(request):
    return web.Response(text=generate_latest(REGISTRY), content_type='text/plain')
```

## 故障排查

### 常见问题

| 问题 | 症状 | 排查步骤 |
|------|------|---------|
| WebSocket 连接失败 | `Connection refused` | 检查防火墙，确保 8770 端口开放；检查 QWEN_WORKSPACE_ID 配置 |
| API 返回 401 | 大模型调用失败 | 验证 QWEN_API_KEY 有效性和配额 |
| 数据库锁定 | `database is locked` | SQLite 并发写入冲突，考虑升级到 PostgreSQL |
| 内存泄漏 | 内存占用不断增长 | 检查 WebSocket 连接是否正常关闭 |
| CORS 错误 | 客户端无法调用 API | 检查 omni/cors.py 的 ALLOWED_ORIGINS 配置 |

### 日志分析

```bash
# 查看最新错误
tail -50 server.log | grep -i error

# 统计错误类型
grep -i error server.log | awk '{print $NF}' | sort | uniq -c

# 监控性能
grep "duration:" server.log | awk '{print $NF}' | sort -n | tail -10
```

## 升级与维护

### 版本更新

```bash
# 拉取最新代码
git pull origin main

# 测试
python3 -m unittest discover -s tests

# 重启服务
systemctl restart omni-server
# 或
docker-compose up -d --force-recreate
```

### 数据库备份

```bash
# 定期备份
cp omni_memory.db omni_memory.db.backup.$(date +%Y%m%d)

# 使用 SQLite 导出
sqlite3 omni_memory.db ".backup omni_memory.db.backup"

# 在 crontab 中定时备份
0 2 * * * cp /app/omni_memory.db /backups/omni_memory.db.$(date +\%Y\%m\%d)
```

### 优雅关闭

```bash
# 给服务足够时间处理现有连接
systemctl stop omni-server  # 有 systemd 配置则会等待

# 或手动 SIGTERM
kill -TERM <pid>
```

## 性能优化

### 缓存策略

考虑为频繁查询的记忆层数据添加缓存（Redis）：

```python
# 可选：集成 Redis
pip install aioredis
```

### 数据库优化

```sql
-- 为常用查询添加索引
CREATE INDEX IF NOT EXISTS idx_memory_user_type ON memory(user_id, type);
CREATE INDEX IF NOT EXISTS idx_memory_timestamp ON memory(timestamp DESC);
```

### 连接池

对于高并发，考虑使用连接池（如使用 PostgreSQL 时的 pgBouncer）。

## 安全考虑

- [ ] 启用 HTTPS（Let's Encrypt 证书）
- [ ] 配置防火墙规则，仅允许必要端口
- [ ] 使用强加密密钥存储 API Key
- [ ] 定期更新依赖库（`pip list --outdated`）
- [ ] 启用 rate limiting 防止滥用
- [ ] 日志中不记录敏感信息（API Key、用户个人信息）
- [ ] 定期安全审计和渗透测试

## 下一步

- 集成数据库连接池（PostgreSQL）
- 设置 CI/CD 流程（GitHub Actions）
- 配置 CDN 加速（仅针对营销站点）
- 建立监控告警系统（Sentry、DataDog）
- 准备灾难恢复方案
