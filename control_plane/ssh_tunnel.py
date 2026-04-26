#!/usr/bin/env python3
"""
SSH Tunnel Manager - Control Plane端（Yggdrasil）

通信方向：Control Plane → Node Agent
- Control Plane（Yggdrasil）主动SSH连接到 Node Agent（HCCS86）
- 建立正向隧道：本地端口 → Node Agent HTTP Server
- 或直接通过SSH执行命令（使用SSH Remote Command Execution）

设计原则：
- Node Agent无需开放端口（防火墙友好）
- Control Plane主动连接（符合网络拓扑）
"""

import os
import subprocess
import time
import signal
import threading
import requests
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

@dataclass
class NodeSSHConfig:
    """节点SSH配置"""
    node_id: str
    ssh_host: str  # HCCS86外网地址
    ssh_port: int  # SSH端口
    ssh_user: str
    ssh_key_path: Optional[str] = None
    node_http_port: int = 8001  # Node Agent HTTP Server端口
    local_tunnel_port: int = 9001  # 本地隧道端口（映射到Node Agent）

class SSHTunnelManager:
    """SSH隧道管理器（Control Plane端）
    
    通信方向：Control Plane → Node Agent
    """
    
    def __init__(self, config: NodeSSHConfig):
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = False
        self.reconnect_count = 0
        self.max_reconnect = 10
    
    def start(self) -> bool:
        """启动SSH隧道（正向隧道：Control Plane → Node Agent）"""
        if self.process and self.process.poll() is None:
            print(f"⚠️ SSH隧道已运行")
            return True
        
        try:
            # 构建SSH命令（正向隧道）
            # ssh -L 9001:localhost:8001 node_host
            # 将本地9001端口转发到Node Agent的8001端口
            cmd = self._build_ssh_command()
            
            print(f"🚀 启动SSH隧道...")
            print(f"   方向: Yggdrasil → {self.config.node_id}")
            print(f"   命令: {' '.join(cmd)}")
            
            # 启动SSH进程
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setpgrp
            )
            
            # 等待隧道建立
            if self._wait_for_tunnel(timeout=30):
                print(f"✅ SSH隧道建立成功（PID={self.process.pid}）")
                print(f"   映射: localhost:{self.config.local_tunnel_port} → {self.config.node_id}:{self.config.node_http_port}")
                
                # 启动监控线程
                self._start_monitor()
                return True
            else:
                print(f"❌ SSH隧道建立失败")
                self._cleanup_failed_start()
                return False
                
        except Exception as e:
            print(f"❌ SSH隧道启动异常: {e}")
            return False
    
    def _build_ssh_command(self) -> list:
        """构建SSH命令（正向隧道）"""
        # 正向隧道：-L local_port:remote_host:remote_port
        # 将本地9001转发到Node Agent的8001
        tunnel_spec = f"{self.config.local_tunnel_port}:localhost:{self.config.node_http_port}"
        
        cmd = [
            "ssh",
            "-N",  # 不执行远程命令
            "-L", tunnel_spec,  # 正向隧道
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no",
            "-p", str(self.config.ssh_port),
            f"{self.config.ssh_user}@{self.config.ssh_host}"
        ]
        
        if self.config.ssh_key_path:
            cmd.extend(["-i", self.config.ssh_key_path])
        
        return cmd
    
    def _wait_for_tunnel(self, timeout: int = 30) -> bool:
        """等待隧道建立"""
        start = time.time()
        
        while time.time() - start < timeout:
            # 检查进程是否存活
            if self.process.poll() is not None:
                return False
            
            # 检查本地端口是否监听
            try:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex(('localhost', self.config.local_tunnel_port))
                sock.close()
                
                if result == 0:
                    return True
            except:
                pass
            
            time.sleep(2)
        
        return False
    
    def _cleanup_failed_start(self):
        """清理失败的启动"""
        if self.process:
            self.process.kill()
            self.process = None
    
    def _start_monitor(self):
        """启动监控线程"""
        self._stop_monitor = False
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True
        )
        self.monitor_thread.start()
    
    def _monitor_loop(self):
        """监控隧道进程"""
        while not self._stop_monitor:
            if self.process:
                poll_result = self.process.poll()
                
                if poll_result is not None:
                    print(f"⚠️ SSH隧道进程已退出 (exit_code={poll_result})")
                    
                    if not self._stop_monitor and self.reconnect_count < self.max_reconnect:
                        self.reconnect_count += 1
                        print(f"🔄 尝试重连（第{self.reconnect_count}次）...")
                        time.sleep(5)
                        self.start()
                    
                    break
            
            time.sleep(10)
    
    def stop(self) -> bool:
        """停止SSH隧道"""
        if not self.process:
            return True
        
        try:
            print(f"🛑 停止SSH隧道...")
            
            self._stop_monitor = True
            if self.monitor_thread:
                self.monitor_thread.join(timeout=5)
            
            self.process.terminate()
            
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            
            self.process = None
            print(f"✅ SSH隧道已停止")
            return True
            
        except Exception as e:
            print(f"❌ SSH隧道停止异常: {e}")
            return False
    
    def is_running(self) -> bool:
        """检查隧道是否运行"""
        return self.process is not None and self.process.poll() is None
    
    def push_command(self, endpoint: str, payload: Dict) -> Optional[Dict]:
        """通过隧道推送指令"""
        if not self.is_running():
            print(f"❌ SSH隧道未运行")
            return None
        
        try:
            response = requests.post(
                f"http://localhost:{self.config.local_tunnel_port}{endpoint}",
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                print(f"⚠️ 推送失败: {response.status_code}")
                return None
        except Exception as e:
            print(f"❌ 推送异常: {e}")
            return None
    
    def get_status(self) -> dict:
        """获取隧道状态"""
        return {
            "node_id": self.config.node_id,
            "running": self.is_running(),
            "pid": self.process.pid if self.process else None,
            "local_port": self.config.local_tunnel_port,
            "reconnect_count": self.reconnect_count
        }

class SSHRemoteExecutor:
    """SSH远程执行器（不建立隧道，直接执行命令）
    
    适用于一次性命令执行（如启动Node Agent）
    """
    
    def __init__(self, config: NodeSSHConfig):
        self.config = config
    
    def execute(self, command: str) -> tuple:
        """远程执行命令"""
        ssh_cmd = [
            "ssh",
            "-p", str(self.config.ssh_port),
            "-o", "StrictHostKeyChecking=no",
            f"{self.config.ssh_user}@{self.config.ssh_host}",
            command
        ]
        
        if self.config.ssh_key_path:
            ssh_cmd.extend(["-i", self.config.ssh_key_path])
        
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            return (result.returncode, result.stdout, result.stderr)
        except subprocess.TimeoutExpired:
            return (-1, "", "Timeout")
        except Exception as e:
            return (-1, "", str(e))
    
    def start_node_agent(self) -> bool:
        """启动Node Agent（远程执行）"""
        # 命令：在HCCS86上启动Node Agent HTTP Server
        command = f"cd /root/gpuhub && python3 node_agent/http_server.py &"
        
        returncode, stdout, stderr = self.execute(command)
        
        if returncode == 0:
            print(f"✅ Node Agent已启动（{self.config.node_id}）")
            return True
        else:
            print(f"❌ Node Agent启动失败: {stderr}")
            return False
    
    def check_node_agent_status(self) -> bool:
        """检查Node Agent状态"""
        command = f"pgrep -f 'http_server.py' || curl -s http://localhost:{self.config.node_http_port}/"
        
        returncode, stdout, stderr = self.execute(command)
        
        return returncode == 0 or "ok" in stdout

# 测试入口
if __name__ == "__main__":
    print("🧪 测试SSH隧道（Control Plane → Node Agent）...")
    
    # 示例配置
    config = NodeSSHConfig(
        node_id="hccs86-01",
        ssh_host="120.209.70.195",
        ssh_port=30218,
        ssh_user="root",
        ssh_key_path=None,
        node_http_port=8001,
        local_tunnel_port=9001
    )
    
    # 方案1：建立隧道
    # manager = SSHTunnelManager(config)
    # manager.start()
    # manager.push_command("/load_model", {"model": "test"})
    
    # 方案2：直接执行命令
    # executor = SSHRemoteExecutor(config)
    # executor.start_node_agent()
    
    print("✅ SSH隧道代码已创建（待测试）")