# GPUHub

**GPU 任务调度平台** — 在 Yggdrasil + HCCS86 基础设施上构建统一 GPU 任务编排层。

GPUHub 是一个轻量级的 GPU 任务调度平台，部署在总控端（Yggdrasil）和算力端（HCCS86）之间，实现多节点统一管理、任务队列、请求跟踪、前端仪表盘。

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│ 上游 LLM     │────▶│  GPUHub          │────▶│  Node Agent  │
│ 网关 / 客户端 │◀────│  Control Plane   │◀────│  (HCCS86)    │
└──────────────┘     │  (Yggdrasil)     │     │  Executors   │
                     └────────┬─────────┘     └──────────────┘
                              │
                     ┌────────▼─────────┐
                     │  Redis + MySQL   │
                     │  队列 + 请求跟踪  │
                     └──────────────────┘
```

## ✨ 核心特性

- **总控端调度**：FastAPI 提供 REST API，Redis 维护任务队列，MySQL 存储请求跟踪
- **算力端代理**：Python Worker 主动连接总控端，上报 GPU 状态，拉取任务执行
- **心跳机制**：Node Agent 每 10 秒上报心跳，总控端实时监控节点状态
- **请求跟踪**：完整状态流转（received → queued → scheduled → running → succeeded/failed）
- **前端仪表盘**：Requests + Nodes + Queues 三个核心页面
- **多任务类型**：支持 chat / embedding / stt 三类任务
- **外网访问**：通过 Cloudflare Tunnel 提供外网访问总控端前端和 API

## 🎯 适用场景

- 单用户多 GPU 服务器管理
- 需要统一调度、请求跟踪、前端监控
- 间歇性 GPU 推理需求，不想手动管理
- 未来计划接入更多 GPU 节点

## 📦 环境要求

### 总控端（Yggdrasil）

- Docker + Docker Compose
- Redis（192.168.1.6:6379）
- MySQL（192.168.1.6:3306）
- Cloudflare Tunnel（外网访问）

### 算力端（HCCS86）

- Python 3.10+（推荐使用 Conda）
- nvidia-smi 可用
- 网络可达总控端（通过 Cloudflare Tunnel）

## 🚀 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/Sen-Yao/gpuhub.git
cd gpuhub
```

### 2. 总控端部署（Yggdrasil）

```bash
# 复制代码到 Yggdrasil
scp -r gpuhub/* Yggdrasil:/mnt/user/appdata/gpuhub/

# SSH 到 Yggdrasil
ssh Yggdrasil

# 初始化数据库
cd /mnt/user/appdata/gpuhub
python3 control_plane/init_db.py

# 启动 Docker
docker-compose up -d

# 配置 Cloudflare Tunnel
# gpuhub.senyao.org → localhost:8001
```

### 3. 算力端部署（HCCS86）

```bash
# 复制代码到 HCCS86
scp -r gpuhub/node_agent/* HCCS86:~/gpuhub/

# SSH 到 HCCS86
ssh HCCS86

# 创建 Conda 环境
cd ~/gpuhub
conda env create -f environment.yml
conda activate gpuhub

# 启动 Agent（后台）
export CONTROL_PLANE_URL=https://gpuhub.senyao.org
export NODE_ID=hccs86-01
nohup python main.py > agent.log 2>&1 &
```

### 4. 测试 API

```bash
# Chat API
curl -X POST https://gpuhub.senyao.org/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.1-8b","messages":[{"role":"user","content":"你好"}]}'

# Embedding API
curl -X POST https://gpuhub.senyao.org/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"text-embedding-3-small","input":"测试文本"}'

# STT API
curl -X POST https://gpuhub.senyao.org/v1/audio/transcriptions \
  -F "file=@audio.wav" \
  -F "model=whisper-large-v3"
```

### 5. 访问仪表盘

浏览器访问：https://gpuhub.senyao.org

## 📁 项目结构

```
gpuhub/
├── README.md               # 项目介绍（GitHub 风格）
├── environment.yml         # Conda 环境配置（算力端）
├── docker-compose.yml      # Docker 编排（总控端）
├── docs/                   # 文档文件夹
│   ├── API.md              # API 协议详细文档
│   ├── ARCHITECTURE.md     # 架构设计文档
│   ├── DEPLOYMENT.md       # 部署指南详细版
│   └── ROADMAP.md          # V1-V3 功能演进
├── control_plane/          # 总控端代码
│   ├── main.py             # FastAPI 入口
│   ├── init_db.py          # 数据库初始化
│   ├── requirements.txt    # Python 依赖
│   └── Dockerfile          # Docker 构建
├── node_agent/             # 算力端代码
│   ├── main.py             # Agent 入口
│   └── requirements.txt    # Python 依赖
└── frontend/               # 前端仪表盘
    └── index.html          # Requests + Nodes + Queues
```

## 📚 文档

详细文档请查看 `docs/` 目录：

- [API 协议](docs/API.md) — 请求状态流转、任务类型定义、心跳协议
- [架构设计](docs/ARCHITECTURE.md) — 总控端 + 算力端架构详解
- [部署指南](docs/DEPLOYMENT.md) — Yggdrasil + HCCS86 部署步骤
- [功能演进](docs/ROADMAP.md) — V1/V2/V3 功能规划

## 🔧 配置

### 总控端配置（环境变量）

```bash
REDIS_HOST=192.168.1.6
REDIS_PORT=6379
MYSQL_HOST=192.168.1.6
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=<password>
MYSQL_DATABASE=gpuhub
```

### 算力端配置（环境变量）

```bash
CONTROL_PLANE_URL=https://gpuhub.senyao.org
NODE_ID=hccs86-01
HEARTBEAT_INTERVAL=10
FETCH_INTERVAL=5
```

## 🗺️ Roadmap

| 版本 | 功能 | 状态 |
|------|------|------|
| **V1** | 单节点 + 基础调度 + 队列 + 前端 | ✅ 当前 |
| **V2** | 多节点 + 故障转移 + 优先级队列 | ⏳ 规划 |
| **V3** | 分片执行 + 多节点协同 + 插件化 | ⏳ 规划 |

详细功能演进见 [docs/ROADMAP.md](docs/ROADMAP.md)。

## 🤝 贡献

当前为单用户项目，暂不接受外部贡献。

## 📄 许可证

MIT License

---

**维护者**: SenYao (林子垚) | **实验室**: SenyaoLab