# GPUHub 部署指南

> **版本**: v1.0
> **总控端端口**: 8003

---

## 一、总控端部署

### 1.1 前置条件

- Docker 已安装
- Redis 已运行
- MySQL 已运行
- 反向代理已配置（Cloudflare Tunnel 或 Nginx）

### 1.2 克隆代码

```bash
# 克隆仓库
cd /your/appdata/path
git clone https://github.com/gpuhub/gpuhub.git
cd gpuhub
```

### 1.3 初始化数据库

```bash
# 安装 MySQL connector（如果没有）
pip install mysql-connector-python

# 运行初始化脚本
python3 control_plane/init_db.py
```

### 1.4 配置环境变量

```bash
# 创建密码文件（敏感信息不进 Git）
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

### 1.5 启动 Docker

```bash
# 构建镜像
docker-compose build

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

---

## 二、算力端部署

### 2.1 前置条件

- Conda 已安装
- nvidia-smi 可用
- 网络可达总控端外网地址

### 2.2 克隆代码

```bash
# 克隆仓库
cd ~
git clone https://github.com/gpuhub/gpuhub.git
cd gpuhub
```

### 2.3 创建 Conda 环境

```bash
# 创建环境
conda env create -f environment.yml

# 激活环境
conda activate gpuhub

# 验证环境
python -c "import requests; print('✅ 环境正确')"
```

### 2.4 启动 Node Agent

```bash
# 设置环境变量
export CONTROL_PLANE_URL=https://your-domain.com
export NODE_ID=worker-node-01
export HEARTBEAT_INTERVAL=10
export FETCH_INTERVAL=5

# 前台运行（调试）
python3 node_agent/main.py

# 后台运行（生产）
nohup python3 node_agent/main.py > agent.log 2>&1 &

# 查看日志
tail -f agent.log
```

### 2.5 systemd 服务（可选）

创建 systemd 服务文件：

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

## 三、集成测试

### 3.1 测试总控端

```bash
# 健康检查
curl http://localhost:8003/health

# 或通过外网
curl https://your-domain.com/health
```

### 3.2 测试算力端心跳

```bash
# 查看算力端日志
tail -20 agent.log

# 应看到「✅ 心跳上报成功」
```

### 3.3 测试前端仪表盘

浏览器访问：https://your-domain.com

检查：
- Requests 页面显示请求列表
- Nodes 页面显示节点状态
- Queues 页面显示队列状态

### 3.4 测试 Chat API

```bash
curl -X POST https://your-domain.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.1-8b","messages":[{"role":"user","content":"你好"}]}'
```

应返回：

```json
{
  "request_id": "uuid-xxx",
  "status": "queued",
  "message": "请求已加入队列"
}
```

---

## 四、故障排查

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| 502 Bad Gateway | Docker 未启动 | `docker-compose up -d` |
| 节点不在线 | Agent 未运行 | 检查 Agent 进程 |
| 队列无任务 | API 端点异常 | 检查 Control Plane 日志 |
| GPU 状态未上报 | nvidia-smi 失败 | 检查 GPU 驱动 |
| MySQL 连接失败 | 密码错误 | 检查 .env 文件 |

---

## 五、日志查看

### 总控端日志

```bash
# Docker logs
docker-compose logs -f gpuhub-control-plane
```

### 算力端日志

```bash
# Node Agent logs
tail -f agent.log

# 或 systemd logs
sudo journalctl -u gpuhub-agent -f
```

---

## 六、停止服务

### 总控端

```bash
docker-compose down
```

### 算力端

```bash
# 后台进程
pkill -f "python.*node_agent/main.py"

# systemd 服务
sudo systemctl stop gpuhub-agent
```

---

_最后更新: 2026-04-22_