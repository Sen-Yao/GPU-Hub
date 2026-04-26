#!/usr/bin/env python3
"""
GPUHub Node Agent v2 - 启动脚本

整合模块：
- ExecutorManager（进程管理）
- HTTP Server（指令接收）
- SSH Tunnel（反向隧道）
- 心跳上报
"""

import os
import sys
import yaml
import time
import threading
import requests
from datetime import datetime
from typing import Dict, List

# 添加模块路径
sys.path.append("/home/openclawvm/.openclaw/workspace/gpuhub/node_agent")

from executor_manager import ExecutorManager, ExecutorConfig
from ssh_tunnel import SSHTunnelManager, SSHTunnelConfig

# ============== 配置加载 ==============

def load_local_config() -> Dict:
    """加载本地配置（models.yaml）"""
    config_path = os.environ.get(
        "GPUHUB_CONFIG",
        "/home/openclawvm/.openclaw/workspace/gpuhub/node_agent/models.yaml"
    )
    
    if not os.path.exists(config_path):
        print(f"⚠️ 配置文件不存在: {config_path}")
        return {}
    
    with open(config_path) as f:
        return yaml.safe_load(f)

# ============== Node Agent v2 ==============

class NodeAgentV2:
    """Node Agent v2 - 自包含推理能力"""
    
    def __init__(self):
        # 加载配置
        self.config = load_local_config()
        
        # 节点ID
        self.node_id = self.config.get("node_id", "hccs86-01")
        
        # 生成实例ID（每次启动生成新的）
        self.instance_id = self._generate_instance_id()
        
        # ExecutorManager
        self.executor_manager = ExecutorManager()
        
        # SSH隧道（可选）
        self.ssh_tunnel: SSHTunnelManager = None
        
        # Control Plane URL
        self.control_plane_url = os.environ.get(
            "CONTROL_PLANE_URL",
            "http://192.168.1.6:8003"
        )
        
        # 心跳上报
        self.heartbeat_thread = None
        self._stop_heartbeat = False
    
    def _generate_instance_id(self) -> str:
        """生成实例ID"""
        import uuid
        return str(uuid.uuid4())
    
    def start(self):
        """启动 Node Agent"""
        print(f"🚀 Node Agent v2 启动")
        print(f"   节点ID: {self.node_id}")
        print(f"   实例ID: {self.instance_id}")
        print(f"   Control Plane: {self.control_plane_url}")
        
        # 1. 启动SSH隧道（如果配置）
        if self.config.get("ssh_tunnel"):
            self._start_ssh_tunnel()
        
        # 2. 启动HTTP Server（后台线程）
        self._start_http_server()
        
        # 3. 启动心跳上报
        self._start_heartbeat()
        
        print("✅ Node Agent v2 已启动")
        
        # 主循环（保持运行）
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            print("\n🛑 收到停止信号")
            self.stop()
    
    def stop(self):
        """停止 Node Agent"""
        print("🛑 停止 Node Agent...")
        
        # 停止心跳
        self._stop_heartbeat = True
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=5)
        
        # 关闭所有Executor
        self.executor_manager.shutdown_all()
        
        # 停止SSH隧道
        if self.ssh_tunnel:
            self.ssh_tunnel.stop()
        
        print("✅ Node Agent 已停止")
    
    def _start_ssh_tunnel(self):
        """启动SSH隧道"""
        ssh_config = self.config.get("ssh_tunnel", {})
        
        tunnel_config = SSHTunnelConfig(
            control_plane_host=ssh_config.get("control_plane_host", "192.168.1.6"),
            control_plane_user=ssh_config.get("control_plane_user", "gpuhub"),
            control_plane_port=ssh_config.get("control_plane_port", 22),
            tunnel_port=self.config.get("tunnel_port", 9001),
            local_port=self.config.get("local_port", 8001),
            ssh_key_path=ssh_config.get("ssh_key_path")
        )
        
        self.ssh_tunnel = SSHTunnelManager(tunnel_config)
        
        if self.ssh_tunnel.start():
            print("✅ SSH隧道已建立")
        else:
            print("❌ SSH隧道建立失败，将使用HTTP轮询模式")
    
    def _start_http_server(self):
        """启动HTTP Server"""
        import subprocess
        
        print("🚀 启动HTTP Server（端口8001）...")
        
        # 后台启动http_server.py
        subprocess.Popen(
            [sys.executable, "/home/openclawvm/.openclaw/workspace/gpuhub/node_agent/http_server.py"],
            preexec_fn=os.setpgrp
        )
        
        # 等待端口监听
        time.sleep(3)
        print("✅ HTTP Server 已启动")
    
    def _start_heartbeat(self):
        """启动心跳上报"""
        self._stop_heartbeat = False
        self.heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True
        )
        self.heartbeat_thread.start()
        print("✅ 心跳上报已启动（每10秒）")
    
    def _heartbeat_loop(self):
        """心跳上报循环"""
        while not self._stop_heartbeat:
            self._send_heartbeat()
            time.sleep(10)
    
    def _send_heartbeat(self):
        """发送心跳"""
        # 获取GPU状态
        gpu_status = self._get_gpu_status()
        
        # 获取已加载模型
        loaded_models = self.executor_manager.get_loaded_models()
        
        # 获取supported_models（从配置）
        supported_models = list(self.config.get("models", {}).keys())
        
        payload = {
            "node_id": self.node_id,
            "instance_id": self.instance_id,
            "timestamp": datetime.utcnow().isoformat(),
            "gpu_status": gpu_status,
            "loaded_models": loaded_models,
            "supported_models": supported_models,
            "running_tasks": [],  # 简化
            "ssh_tunnel_status": self.ssh_tunnel.get_status() if self.ssh_tunnel else None
        }
        
        try:
            response = requests.post(
                f"{self.control_plane_url}/heartbeat",
                json=payload,
                timeout=10
            )
            
            if response.status_code == 200:
                print(f"✅ 心跳上报成功（loaded_models={len(loaded_models)}）")
            else:
                print(f"⚠️ 心跳上报失败: {response.status_code}")
        except Exception as e:
            print(f"❌ 心跳上报异常: {e}")
    
    def _get_gpu_status(self) -> List[Dict]:
        """获取GPU状态"""
        import subprocess
        
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True
            )
            
            gpu_status = []
            for line in result.stdout.strip().split('\n'):
                parts = line.split(',')
                if len(parts) == 3:
                    gpu_id = int(parts[0].strip())
                    memory_used = int(parts[1].strip())
                    memory_total = int(parts[2].strip())
                    gpu_status.append({
                        "gpu_id": gpu_id,
                        "memory_used": memory_used,
                        "memory_total": memory_total,
                        "memory_free": memory_total - memory_used
                    })
            
            return gpu_status
        except Exception as e:
            print(f"❌ 获取GPU状态失败: {e}")
            return []

# ============== 主入口 ==============

if __name__ == "__main__":
    agent = NodeAgentV2()
    agent.start()