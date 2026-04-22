#!/bin/bash
# GPUHub Node Agent 启动脚本

cd ~/gpuhub/node_agent

export CONTROL_PLANE_URL="https://gpuhub.senyao.org"
export NODE_ID="hccs86-01"
export HEARTBEAT_INTERVAL="10"
export FETCH_INTERVAL="5"

# 停止旧进程
pkill -f "python3 main.py" || true
sleep 1

# 启动
nohup python3 -u main.py > ~/agent.log 2>&1 &

sleep 2
PID=$(pgrep -f "python3 main.py")
if [ -n "$PID" ]; then
    echo "✅ Node Agent 已启动 (PID=$PID)"
    echo "日志: ~/agent.log"
    tail -5 ~/agent.log
else
    echo "❌ Node Agent 启动失败"
fi