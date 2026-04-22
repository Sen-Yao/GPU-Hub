# GPUHub 部署指南

> **版本**: v1.0
> **总控端端口**: 8001

---

## 一、总控端部署（Yggdrasil）

### 1.1 前置条件

- Docker 已安装
- Redis 已运行（192.168.1.6:6379）
- MySQL 已运行（192.168.1.6:3306）
- Cloudflare Tunnel 已配置

### 1.2 克隆代码

```bash
# SSH 到 Yggdrasil
ssh Yggdrasil

# 克隆仓库
cd /mnt/user/appdata
git clone https://github.com/Sen-Yao/gpuhub.git
cd gpuhub
```

### 1.3 初始化数据库

```bash
# 安装 MySQL connector（如果没有）
pip install mysql-connector-python

# 运行初始化脚本
python3 control_plane/init_db.py
```

### 1.4 配置 MySQL 密码

```bash
# 创建密码文件（敏感信息不进 Git）
echo "你的MySQL密码" > mysql_password.txt

# 或设置环境变量
export MYSQL_PASSWORD="你的MySQL密码"
```

### 1.5 启动 Docker（两种方式）

#### 方式A：docker-compose

```bash
# 构建镜像
docker-compose build

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

#### 方式B：unRAID WebUI

1. 打开 unRAID WebUI → Docker → Add Container
2. 点击「Template from User」
3. 选择「GPUHub」模板
4. 填写 MySQL 密码
5. 点击「Apply」启动

### 1.6 配置 Cloudflare Tunnel

在 Yggdrasil 的 Cloudflare Tunnel 配置中添加：

```yaml
# gpuhub.senyao.org → localhost:8001
- hostname: gpuhub.senyao.org
  service: http://localhost:8001
```

---

## 二、算力端部署（HCCS86）

### 2.1 前置条件

- Conda 已安装（默认算力端均有）
- nvidia-smi 可用
- 网络可达 gpuhub.senyao.org（通过 Cloudflare Tunnel）

### 2.2 克隆代码

```bash
# SSH 到 HCCS86
ssh HCCS86

# 克隆仓库
cd ~
git clone https://github.com/Sen-Yao/gpuhub.git
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
export CONTROL_PLANE_URL=https://gpuhub.senyao.org
export NODE_ID=hccs86-01
export HEARTBEAT_INTERVAL=10
export FETCH_INTERVAL=5

# 前台运行（调试）
python3 node_agent/main.py

# 后台运行（生产）
nohup python3 node_agent/main.py > node_agent.log 2>&1 &

# 查看日志
tail -f node_agent.log
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
User=linziyao
WorkingDirectory=/home/linziyao/gpuhub
Environment="CONTROL_PLANE_URL=https://gpuhub.senyao.org"
Environment="NODE_ID=hccs86-01"
ExecStart=/home/linziyao/miniconda3/envs/gpuhub/bin/python node_agent/main.py
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
curl http://localhost:8001/health

# 或通过外网
curl https://gpuhub.senyao.org/health
```

### 3.2 测试算力端心跳

```bash
# 查看算力端日志
ssh HCCS86 "tail -20 ~/gpuhub/node_agent.log"

# 应看到「✅ 心跳上报成功」
```

### 3.3 测试前端仪表盘

浏览器访问：https://gpuhub.senyao.org

检查：
- Requests 页面显示请求列表
- Nodes 页面显示节点状态（hccs86-01）
- Queues 页面显示队列状态

### 3.4 测试 Chat API

```bash
curl -X POST https://gpuhub.senyao.org/v1/chat/completions \
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

### 3.5 测试 Embedding API

```bash
curl -X POST https://gpuhub.senyao.org/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"text-embedding-3-small","input":"测试文本"}'
```

---

## 四、故障排查

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| 502 Bad Gateway | Docker 未启动 | `docker-compose up -d` |
| 节点不在线 | Agent 未运行 | 检查 HCCS86 上的进程 |
| 队列无任务 | API 端点异常 | 检查 Control Plane 日志 |
| GPU 状态未上报 | nvidia-smi 失败 | 检查 GPU 驱动 |
| MySQL 连接失败 | 密码错误 | 检查 mysql_password.txt |

---

## 五、日志查看

### 总控端日志

```bash
# Docker logs
docker-compose logs -f gpuhub-control-plane

# 或直接查看容器日志
docker logs gpuhub-control-plane
```

### 算力端日志

```bash
# Node Agent logs
tail -f ~/gpuhub/node_agent.log

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