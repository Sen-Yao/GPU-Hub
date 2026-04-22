# GPUHub 使用文档

> **版本**: v1.0  
> **更新时间**: 2026-04-22

---

## 目录

1. [系统架构](#1-系统架构)
2. [API 接口](#2-api-接口)
3. [前端仪表盘](#3-前端仪表盘)
4. [部署指南](#4-部署指南)
5. [运维手册](#5-运维手册)
6. [故障排查](#6-故障排查)
7. [安全说明](#7-安全说明)

---

## 1. 系统架构

### 1.1 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      Client Layer                            │
│  (外部应用 / 用户) → HTTP Request                            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│              总控端 - Control Plane (Docker)                 │
│  ┌─────────────┬─────────────┬─────────────┬─────────────┐  │
│  │ FastAPI     │ Redis Queue │ MySQL DB    │ 前端仪表盘  │  │
│  │ (调度)      │ (队列)      │ (请求跟踪)  │ (监控)      │  │
│  └─────────────┴─────────────┴─────────────┴─────────────┘  │
│  端口: 8003 | 外网: your-domain.com                         │
└─────────────────────────────────────────────────────────────┘
                              ↓ (HTTP/WebSocket 心跳)
┌─────────────────────────────────────────────────────────────┐
│              算力端 - Worker Node (Python Worker)            │
│  ┌─────────────┬─────────────┬─────────────┐               │
│  │ Node Agent  │ Executors   │ GPU Monitor │               │
│  │ (心跳上报)  │ (llama/whisper)│ (nvidia-smi)│               │
│  └─────────────┴─────────────┴─────────────┘               │
│  NVIDIA GPU (48GB+ each)                                    │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 组件说明

| 组件 | 说明 |
|------|------|
| **总控端** | 任务调度、队列管理、请求跟踪、前端仪表盘 |
| **算力端** | GPU 任务执行、心跳上报 |
| **Redis** | 任务队列存储 |
| **MySQL** | 请求状态跟踪 |
| **反向代理** | 外网访问（Cloudflare Tunnel / Nginx）|

---

## 2. API 接口

### 2.1 基础信息

| 项目 | 内容 |
|------|------|
| **Base URL** | `https://your-domain.com` |
| **Content-Type** | `application/json` |
| **认证** | V1 暂无（V2 添加 API Key）|

### 2.2 Chat API

**端点**: `/v1/chat/completions`

**请求**:
```bash
curl -X POST https://your-domain.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.1-8b",
    "messages": [
      {"role": "user", "content": "你好"}
    ],
    "temperature": 0.7,
    "max_tokens": 2048
  }'
```

**响应**:
```json
{
  "request_id": "uuid-xxx",
  "status": "queued",
  "message": "请求已加入队列"
}
```

### 2.3 Embedding API

**端点**: `/v1/embeddings`

**请求**:
```bash
curl -X POST https://your-domain.com/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{
    "model": "text-embedding-3-small",
    "input": "这是一段文本"
  }'
```

### 2.4 STT API

**端点**: `/v1/audio/transcriptions`

**请求**:
```bash
curl -X POST https://your-domain.com/v1/audio/transcriptions \
  -H "Content-Type: multipart/form-data" \
  -F "file=@audio.mp3" \
  -F "model=whisper-large-v3"
```

### 2.5 查询请求状态

**端点**: `/v1/requests/{request_id}`

**请求**:
```bash
curl https://your-domain.com/v1/requests/{request_id}
```

**响应**:
```json
{
  "request_id": "xxx",
  "status": "succeeded",
  "result": {
    "content": "你好！有什么我可以帮助你的吗？"
  }
}
```

### 2.6 健康检查

**端点**: `/health`

```bash
curl https://your-domain.com/health
```

**响应**:
```json
{
  "status": "healthy",
  "redis": true,
  "mysql": true
}
```

---

## 3. 前端仪表盘

### 3.1 访问地址

浏览器访问：`https://your-domain.com`

### 3.2 功能页面

| 页面 | 说明 |
|------|------|
| **Requests** | 请求列表、状态查询 |
| **Nodes** | GPU 节点状态、显存使用率 |
| **Queues** | 任务队列、排队位置 |

### 3.3 界面设计

- **风格**: Industrial/Utilitarian（工业风格）
- **主题**: 深色背景 + 绿色数据点
- **字体**: JetBrains Mono + Space Mono
- **自动刷新**: 每 10 秒更新数据

---

## 4. 部署指南

### 4.1 前置条件

| 服务 | 说明 |
|------|------|
| **Redis** | 需要预先部署（默认端口 6379）|
| **MySQL** | 需要预先部署（默认端口 3306）|
| **反向代理** | Cloudflare Tunnel 或 Nginx |

### 4.2 总控端部署

**步骤 1: 克隆代码**
```bash
cd /your/appdata/path
git clone https://github.com/gpuhub/gpuhub.git
cd gpuhub
```

**步骤 2: 创建环境变量文件**
```bash
cat > .env << EOF
GPUHUB_PORT=8003
REDIS_HOST=your.redis.host
REDIS_PORT=6379
REDIS_PASSWORD=
MYSQL_HOST=your.mysql.host
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=gpuhub
EOF
```

**步骤 3: 初始化数据库**
```bash
cat docs/init.sql | mysql -uroot -pyour_password
```

**步骤 4: 启动 Docker**
```bash
docker-compose build
docker-compose up -d
docker-compose ps
```

**步骤 5: 验证服务**
```bash
curl http://localhost:8003/health
```

### 4.3 算力端部署

**步骤 1: 克隆代码**
```bash
cd ~
git clone https://github.com/gpuhub/gpuhub.git
cd gpuhub
```

**步骤 2: 创建 Conda 环境**
```bash
conda env create -f environment.yml
conda activate gpuhub
```

**步骤 3: 启动 Agent**
```bash
export CONTROL_PLANE_URL='https://your-domain.com'
export NODE_ID='worker-node-01'
nohup python node_agent/main.py > agent.log 2>&1 &
```

**步骤 4: 查看日志**
```bash
tail -f agent.log
```

### 4.4 systemd 服务（推荐）

创建 systemd 配置：
```bash
sudo nano /etc/systemd/system/gpuhub-agent.service
```

内容：
```ini
[Unit]
Description=GPUHub Node Agent
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/gpuhub
Environment="CONTROL_PLANE_URL=https://your-domain.com"
Environment="NODE_ID=worker-node-01"
ExecStart=/path/to/conda/env/bin/python node_agent/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable gpuhub-agent
sudo systemctl start gpuhub-agent
sudo systemctl status gpuhub-agent
```

---

## 5. 运维手册

### 5.1 日常操作

| 操作 | 命令 |
|------|------|
| **查看总控端日志** | `docker logs gpuhub-control-plane -f` |
| **查看算力端日志** | `tail -f agent.log` |
| **重启总控端** | `docker-compose restart` |
| **重启算力端** | `sudo systemctl restart gpuhub-agent` |
| **检查 GPU 状态** | `nvidia-smi` |

### 5.2 监控指标

| 指标 | 告警阈值 |
|------|---------|
| **GPU 显存占用** | ≥ 46GB → 节点满载 |
| **队列长度** | ≥ 100 → 任务积压 |
| **请求失败率** | ≥ 10% → 异常告警 |
| **节点心跳** | 60秒无心跳 → 节点离线 |

### 5.3 日志查看

**总控端日志**:
```bash
docker logs gpuhub-control-plane --tail 100
```

**算力端日志**:
```bash
tail -100 agent.log
# 或 systemd 日志
sudo journalctl -u gpuhub-agent --tail 100
```

---

## 6. 故障排查

### 6.1 常见问题

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| **API 返回 502** | Docker 未启动 | `docker-compose up -d` |
| **节点不在线** | Agent 未运行 | `systemctl start gpuhub-agent` |
| **队列无任务** | Redis 连接失败 | 检查 REDIS_HOST 配置 |
| **GPU 状态未上报** | nvidia-smi 失败 | 检查 GPU 驱动 |
| **MySQL 连接失败** | 密码错误 | 检查 .env 中 MYSQL_PASSWORD |
| **端口冲突** | 端口被占用 | 修改 GPUHUB_PORT |

### 6.2 检查步骤

1. **检查服务状态**
```bash
curl https://your-domain.com/health

# 算力端
ps aux | grep node_agent
```

2. **检查数据库连接**
```bash
mysql -uroot -p密码 -e "USE gpuhub; SHOW TABLES;"
```

3. **检查 Redis 连接**
```bash
redis-cli -h your.redis.host ping
```

4. **检查日志错误**
```bash
docker logs gpuhub-control-plane | grep ERROR
```

---

## 7. 安全说明

### 7.1 敏感信息管理

**⚠️ 重要规则**:

| 禁止 | 正确做法 |
|------|---------|
| 硬编码密码到代码 | 使用 `.env` 文件 |
| 提交 `.env` 到 Git | 确保 `.gitignore` 排除 `.env` |
| 在容器外暴露密码 | 使用 Docker secrets（V2）|

### 7.2 .env 文件示例

```bash
# GPUHub 环境变量配置
# ⚠️ 此文件不提交到 Git

# GPUHub 服务端口
GPUHUB_PORT=8003

# Redis 配置
REDIS_HOST=your.redis.host
REDIS_PORT=6379
REDIS_PASSWORD=

# MySQL 配置（必须填写）
MYSQL_HOST=your.mysql.host
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_mysql_password
MYSQL_DATABASE=gpuhub
```

### 7.3 .gitignore 确认

确保以下内容在 `.gitignore` 中：
```gitignore
# Environment variables
.env
.env.local
mysql_password.txt
```

---

## 8. 版本演进

### V1 功能（当前）

| 功能 | 状态 |
|------|------|
| 单节点接入 | ✅ 完成 |
| 基础调度 | ✅ 完成 |
| 任务队列 | ✅ 完成 |
| 前端仪表盘 | ✅ 完成 |
| 外网访问 | ✅ 完成 |

### V2 功能（规划）

| 功能 | 说明 |
|------|------|
| 多节点接入 | 第 2+ 个算力端 |
| Tailscale 集成 | overlay 网络 |
| API Key 认证 | 用户级权限 |
| 配额管理 | 资源限制 |

详见：`docs/ROADMAP.md`

---

## 9. 相关链接

| 项目 | 链接 |
|------|------|
| **GitHub** | https://github.com/gpuhub/gpuhub |
| **前端仪表盘** | https://your-domain.com |
| **API 文档** | `docs/API.md` |
| **架构设计** | `docs/ARCHITECTURE.md` |
| **部署指南** | `docs/DEPLOYMENT.md` |
| **Roadmap** | `docs/ROADMAP.md` |

---

_最后更新: 2026-04-22_