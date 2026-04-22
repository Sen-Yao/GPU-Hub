# GPUHub 部署指南

## 一、总控端部署（Yggdrasil）

### 1.1 前置条件

- Docker 已安装
- Redis 已运行（192.168.1.6:6379）
- MySQL 已运行（192.168.1.6:3306）

### 1.2 初始化数据库

```bash
# SSH 到 Yggdrasil
ssh Yggdrasil

# 运行初始化脚本
cd /mnt/user/appdata/gpuhub
python3 control_plane/init_db.py
```

### 1.3 启动 Control Plane

```bash
# 创建目录
mkdir -p /mnt/user/appdata/gpuhub

# 复制代码
scp -r gpuhub/* Yggdrasil:/mnt/user/appdata/gpuhub/

# 启动 Docker
cd /mnt/user/appdata/gpuhub
docker-compose up -d
```

### 1.4 配置 Cloudflare Tunnel

在 Yggdrasil 的 Cloudflare Tunnel 配置中添加：

```yaml
# gpuhub.senyao.org → localhost:8000
- hostname: gpuhub.senyao.org
  service: http://localhost:8000
```

---

## 二、算力端部署（HCCS86）

### 2.1 前置条件

- Python 3.10+ 已安装
- nvidia-smi 可用
- 网络可达 Yggdrasil（通过 Cloudflare Tunnel）

### 2.2 安装依赖

```bash
# SSH 到 HCCS86
ssh HCCS86

# 创建目录
mkdir -p ~/gpuhub

# 复制代码
scp -r gpuhub/node_agent/* HCCS86:~/gpuhub/

# 安装依赖
pip install -r ~/gpuhub/requirements.txt
```

### 2.3 启动 Node Agent

```bash
# 设置环境变量
export CONTROL_PLANE_URL=https://gpuhub.senyao.org
export NODE_ID=hccs86-01

# 启动 Agent
python3 ~/gpuhub/main.py
```

### 2.4 后台运行（可选）

```bash
# 使用 nohup
nohup python3 ~/gpuhub/main.py > ~/gpuhub/agent.log 2>&1 &

# 或使用 systemd（推荐）
```

---

## 三、集成测试

### 3.1 测试 API

```bash
# Chat API
curl -X POST https://gpuhub.senyao.org/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.1-8b","messages":[{"role":"user","content":"你好"}]}'

# Embedding API
curl -X POST https://gpuhub.senyao.org/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"text-embedding-3-small","input":"测试文本"}'
```

### 3.2 测试前端

访问：https://gpuhub.senyao.org

检查：
- Requests 页面显示请求列表
- Nodes 页面显示节点状态
- Queues 页面显示队列状态

---

## 四、故障排查

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| 502 Bad Gateway | Docker 未启动 | `docker-compose up -d` |
| 节点不在线 | Agent 未运行 | 检查 HCCS86 上的进程 |
| 队列无任务 | API 端点异常 | 检查 Control Plane 日志 |
| GPU 状态未上报 | nvidia-smi 失败 | 检查 GPU 驱动 |

---

_最后更新: 2026-04-22_