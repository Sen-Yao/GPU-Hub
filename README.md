# GPUHub

> **GPU 任务调度平台** — 通用基础设施上的统一 GPU 任务编排层。

GPUHub 是一个轻量级的 GPU 任务调度平台，部署在总控端（Control Plane）和算力端（Worker Node）之间，实现多节点统一管理、任务队列、请求跟踪、前端仪表盘。

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| **统一入口** | OpenAI-compatible API，客户端无需关心后端拓扑 |
| **智能调度** | FIFO + GPU 显存判断，自动选择最优节点 |
| **实时监控** | 前端仪表盘（Requests / Nodes / Queues）|
| **请求跟踪** | 完整状态流转记录（MySQL 存储）|
| **轻量算力端** | Python Worker，无需 Docker，直接访问 GPU |

---

## 📐 架构

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│ 网关 / 客户端 │◀────│  Control Plane   │◀────│  Worker Node │
│              │     │  (总控端)         │     │  (算力端)    │
│              │     │                  │     │  Executors   │
└──────────────┘     │  FastAPI         │     │  (llama)     │
                     │  Redis Queue     │     │  (whisper)   │
                     │  MySQL Tracking  │     │              │
                     │  Frontend        │     │  GPU Monitor │
                     └──────────────────┘     └──────────────┘
```

---

## 🚀 快速开始

### 1. 克隆代码

```bash
git clone https://github.com/gpuhub/gpuhub.git
cd gpuhub
```

### 2. 配置环境变量

```bash
cat > .env << EOF
GPUHUB_PORT=8003
REDIS_HOST=your.redis.host
REDIS_PORT=6379
MYSQL_HOST=your.mysql.host
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=gpuhub
EOF
```

### 3. 初始化数据库

```bash
cat docs/init.sql | mysql -uroot -pyour_password
```

### 4. 启动总控端

```bash
docker-compose build
docker-compose up -d
curl http://localhost:8003/health
```

### 5. 启动算力端

```bash
conda env create -f environment.yml
conda activate gpuhub
export CONTROL_PLANE_URL=https://your-domain.com
export NODE_ID=worker-node-01
nohup python node_agent/main.py > agent.log 2>&1 &
```

---

## 📚 文档

| 文档 | 说明 |
|------|------|
| [`docs/USAGE.md`](docs/USAGE.md) | 详细使用指南 |
| [`docs/API.md`](docs/API.md) | API 协议定义 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 架构设计文档 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | 部署指南 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 版本演进规划 |

---

## 🔌 API 端点

| 端点 | 用途 |
|------|------|
| `/v1/chat/completions` | Chat API（OpenAI-compatible）|
| `/v1/embeddings` | Embedding API |
| `/v1/audio/transcriptions` | STT API |
| `/dashboard/requests` | 请求列表 |
| `/dashboard/nodes` | 节点状态 |
| `/dashboard/queues` | 队列状态 |

---

## 🎯 适用场景

- **科研实验**: WandB Sweep、大规模推理实验
- **API 网关**: 统一多个 GPU 服务器入口
- **算力共享**: 多用户共享 GPU 集群

---

## 📦 技术栈

| 模块 | 技术 |
|------|------|
| **总控端** | FastAPI + Redis + MySQL + Docker |
| **算力端** | Python Worker + Conda |
| **前端** | HTML + Tailwind CSS（Industrial 风格）|
| **通信** | HTTP/WebSocket |

---

## 🔐 安全说明

- 所有敏感信息通过环境变量配置（`.env`）
- `.env` 文件不提交到 Git（`.gitignore` 排除）
- MySQL 密码不在代码中硬编码

---

## 📄 License

MIT License

---

_最后更新: 2026-04-22_