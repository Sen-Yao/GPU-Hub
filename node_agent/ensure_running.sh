#!/bin/bash
# GPUHub Node Agent 启动检查脚本
# 添加到 .bashrc 或手动运行

AGENT_LOG=~/agent.log
PID_FILE=~/.gpuhub-agent.pid

# 检查是否已运行
if pgrep -f "python3 main.py" > /dev/null; then
    echo "✅ Node Agent 已运行"
    exit 0
fi

# 启动
cd ~/gpuhub/node_agent
export CONTROL_PLANE_URL="https://gpuhub.senyao.org"
export NODE_ID="hccs86-01"
export HEARTBEAT_INTERVAL="10"
export FETCH_INTERVAL="5"

nohup python3 -u main.py > $AGENT_LOG 2>&1 &
echo $! > $PID_FILE

sleep 2
if pgrep -f "python3 main.py" > /dev/null; then
    echo "✅ Node Agent 已启动 (PID=$(cat $PID_FILE))"
    echo "日志: $AGENT_LOG"
else
    echo "❌ Node Agent 启动失败"
fi