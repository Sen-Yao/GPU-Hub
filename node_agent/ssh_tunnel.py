#!/usr/bin/env python3
"""
SSH Tunnel Manager - SSH反向隧道管理

职责：
- 启动 autossh 建立反向隧道
- 监控隧道进程状态
- 自动重连隧道
"""

import os
import subprocess
import time
import signal
import threading
from typing import Optional
from dataclasses import dataclass
from datetime import datetime

@dataclass
class SSHTunnelConfig:
    """SSH隧道配置"""
    control_plane_host: str  # 总控端主机
    control_plane_user: str  # SSH用户
    control_plane_port: int  # SSH端口
    tunnel_port: int  # 隧道端口（总控端）
    local_port: int  # 本地端口（Node Agent）
    ssh_key_path: Optional[str] = None  # SSH密钥路径

class SSHTunnelManager:
    """SSH隧道管理器"""
    
    def __init__(self, config: SSHTunnelConfig):
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = False
        self.reconnect_count = 0
        self.max_reconnect = 10
    
    def start(self) -> bool:
        """启动SSH隧道"""
        if self.process and self.process.poll() is None:
            print(f"⚠️ SSH隧道已运行")
            return True
        
        try:
            # 构建autossh命令
            cmd = self._build_autossh_command()
            
            print(f"🚀 启动SSH隧道...")
            print(f"   命令: {' '.join(cmd)}")
            
            # 启动autossh进程
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setpgrp
            )
            
            # 等待隧道建立
            if self._wait_for_tunnel(timeout=30):
                print(f"✅ SSH隧道建立成功（PID={self.process.pid}）")
                print(f"   隧道映射: {self.config.tunnel_port} → localhost:{self.config.local_port}")
                
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
    
    def stop(self) -> bool:
        """停止SSH隧道"""
        if not self.process:
            return True
        
        try:
            print(f"🛑 停止SSH隧道...")
            
            # 停止监控
            self._stop_monitor = True
            if self.monitor_thread:
                self.monitor_thread.join(timeout=5)
            
            # 发送 SIGTERM
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
    
    def _build_autossh_command(self) -> list:
        """构建autossh命令"""
        # autossh 参数
        # -M 0: 禁用autossh内置监控（用SSH ServerAliveInterval）
        # -N: 不执行远程命令
        # -R: 反向隧道
        # ServerAliveInterval=10: 每10秒发送心跳
        # ServerAliveCountMax=3: 3次失败后断开
        
        tunnel_spec = f"{self.config.tunnel_port}:localhost:{self.config.local_port}"
        target = f"{self.config.control_plane_user}@{self.config.control_plane_host}"
        
        cmd = [
            "autossh",
            "-M", "0",
            "-N",
            "-R", tunnel_spec,
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no",
            "-p", str(self.config.control_plane_port),
            target
        ]
        
        # 如果指定SSH密钥
        if self.config.ssh_key_path:
            cmd.extend(["-i", self.config.ssh_key_path])
        
        return cmd
    
    def _wait_for_tunnel(self, timeout: int = 30) -> bool:
        """等待隧道建立"""
        start = time.time()
        
        while time.time() - start < timeout:
            # 检查进程是否存活
            if self.process.poll() is not None:
                # 进程已退出
                return False
            
            # 检查隧道是否可用（通过本地端口）
            try:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex(('localhost', self.config.local_port))
                sock.close()
                
                if result == 0:
                    # 本地端口监听（隧道可能已建立）
                    # 实际验证需要总控端推送测试
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
                    # 隧道进程退出
                    print(f"⚠️ SSH隧道进程已退出 (exit_code={poll_result})")
                    
                    # 自动重连
                    if not self._stop_monitor and self.reconnect_count < self.max_reconnect:
                        self.reconnect_count += 1
                        print(f"🔄 尝试重连SSH隧道（第{self.reconnect_count}次）...")
                        
                        # 等待5秒后重连
                        time.sleep(5)
                        
                        if self.start():
                            print(f"✅ SSH隧道重连成功")
                        else:
                            print(f"❌ SSH隧道重连失败")
                    
                    break
            
            time.sleep(10)
    
    def is_running(self) -> bool:
        """检查隧道是否运行"""
        return self.process is not None and self.process.poll() is None
    
    def get_status(self) -> dict:
        """获取隧道状态"""
        return {
            "running": self.is_running(),
            "pid": self.process.pid if self.process else None,
            "tunnel_port": self.config.tunnel_port,
            "local_port": self.config.local_port,
            "reconnect_count": self.reconnect_count
        }

# 测试入口
if __name__ == "__main__":
    print("🧪 测试SSH隧道...")
    
    # 示例配置（需要实际配置）
    config = SSHTunnelConfig(
        control_plane_host="192.168.1.6",  # Yggdrasil
        control_plane_user="gpuhub",
        control_plane_port=22,
        tunnel_port=9001,
        local_port=8001,
        ssh_key_path=None
    )
    
    manager = SSHTunnelManager(config)
    
    # 启动隧道
    # manager.start()
    
    # 查看状态
    # print(manager.get_status())
    
    # 停止
    # manager.stop()
    
    print("✅ SSH隧道代码已创建（待测试）")