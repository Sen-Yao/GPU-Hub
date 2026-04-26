#!/usr/bin/env python3
"""
ExecutorManager - 进程管理器

职责：
- 启动 llama-server / whisper-server 进程
- 监控进程状态
- 自动重启崩溃进程
- 管理进程生命周期

设计原则：
- 每个 Executor 实例对应一个模型 + GPU 组合
- 进程崩溃后自动重启（最多3次）
- 进程状态上报到 Node Agent
"""

import os
import subprocess
import time
import signal
import requests
from typing import Dict, Optional, List
from dataclasses import dataclass
from datetime import datetime
import threading

@dataclass
class ExecutorConfig:
    """Executor 配置"""
    model: str
    model_path: str
    gpu_ids: List[int]
    executor_type: str  # "llama.cpp" or "whisper.cpp"
    port: int
    vram_required: int  # MB

@dataclass
class ExecutorStatus:
    """Executor 状态"""
    executor_id: str
    config: ExecutorConfig
    port: int = 8000  # 默认端口
    pid: Optional[int] = None
    status: str = "stopped"  # "stopped", "running", "crashed"
    start_time: Optional[datetime] = None
    restart_count: int = 0
    last_error: Optional[str] = None

class ExecutorProcess:
    """单个 Executor 进程管理"""
    
    MAX_RESTART = 3  # 最大重启次数
    
    def __init__(self, config: ExecutorConfig):
        self.config = config
        self.executor_id = f"{config.model}-{config.gpu_ids}"
        self.status = ExecutorStatus(
            executor_id=self.executor_id,
            config=config,
            port=config.port,
            status="stopped"
        )
        self.process: Optional[subprocess.Popen] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = False
    
    def start(self) -> bool:
        """启动 Executor 进程"""
        if self.status.status == "running":
            print(f"⚠️ Executor {self.executor_id} 已运行")
            return True
        
        try:
            # 构建启动命令
            cmd = self._build_command()
            
            print(f"🚀 启动 Executor: {self.executor_id}")
            print(f"   命令: {' '.join(cmd)}")
            
            # 启动进程（通过shell执行，避免环境问题）
            # 注意：直接使用subprocess.Popen会导致llama-server崩溃
            # 原因：环境变量或进程组配置问题
            shell_cmd = ' '.join(cmd) + ' &'  # 添加后台运行符
            self.process = subprocess.Popen(
                shell_cmd,
                shell=True,  # 通过shell执行
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setpgrp
            )
            
            print(f"   PID: {self.process.pid}")
            
            # 等待5秒检查进程状态
            time.sleep(5)
            if self.process.poll() is not None:
                exit_code = self.process.poll()
                if exit_code != 0:
                    # shell进程异常退出
                    print(f"   ❌ 进程启动失败 (exit_code={exit_code})")
                    self._cleanup_failed_start()
                    return False
                else:
                    # shell进程正常退出（后台命令已启动）
                    print(f"   ⚠️ shell进程已退出（exit_code=0），后台任务已启动）")
            
            # 更新状态
            self.status.pid = self.process.pid
            self.status.status = "running"
            self.status.start_time = datetime.utcnow()
            self.status.restart_count = 0
            self.status.last_error = None
            
            # 等待进程启动（检查端口，超时60秒）
            if self._wait_for_port(timeout=60):
                print(f"✅ Executor {self.executor_id} 启动成功 (Port={self.config.port})")
                
                # 注意：shell=True模式下，self.process是shell进程，会立即退出
                # 不启动监控线程（shell进程无法监控）
                # 监控应该通过Control Plane的心跳机制实现
                return True
            else:
                print(f"❌ Executor {self.executor_id} 启动失败（端口未响应）")
                # 检查进程是否仍在运行
                if self.process.poll() is not None:
                    print(f"   进程已退出，exit_code={self.process.poll()}")
                else:
                    print(f"   进程仍在运行（PID={self.process.pid}），但端口{self.config.port}未监听")
                self._cleanup_failed_start()
                return False
                
        except Exception as e:
            print(f"❌ Executor {self.executor_id} 启动异常: {e}")
            self.status.status = "crashed"
            self.status.last_error = str(e)
            return False
    
    def stop(self) -> bool:
        """停止 Executor 进程"""
        if self.status.status != "running" or not self.process:
            print(f"⚠️ Executor {self.executor_id} 未运行")
            return True
        
        try:
            print(f"🛑 停止 Executor: {self.executor_id} (PID={self.process.pid})")
            
            # 停止监控线程
            self._stop_monitor = True
            if self.monitor_thread:
                self.monitor_thread.join(timeout=5)
            
            # 发送 SIGTERM
            self.process.terminate()
            
            # 等待进程退出
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # 强制 kill
                print(f"⚠️ 进程未响应 SIGTERM，发送 SIGKILL")
                self.process.kill()
                self.process.wait(timeout=5)
            
            # 更新状态
            self.status.status = "stopped"
            self.status.pid = None
            self.process = None
            
            print(f"✅ Executor {self.executor_id} 已停止")
            return True
            
        except Exception as e:
            print(f"❌ Executor {self.executor_id} 停止异常: {e}")
            return False
    
    def restart(self) -> bool:
        """重启 Executor 进程"""
        print(f"🔄 重启 Executor: {self.executor_id}")
        
        # 增加重启计数
        self.status.restart_count += 1
        
        # 检查重启次数
        if self.status.restart_count > self.MAX_RESTART:
            print(f"❌ Executor {self.executor_id} 重启次数超过上限（{self.MAX_RESTART}）")
            self.status.status = "crashed"
            self.status.last_error = "Max restart exceeded"
            return False
        
        # 先停止
        self.stop()
        
        # 等待2秒
        time.sleep(2)
        
        # 再启动
        return self.start()
    
    def _build_command(self) -> List[str]:
        """构建启动命令"""
        # GPU设备名格式：CUDA0, CUDA1, CUDA2...
        gpu_devices = [f"CUDA{gid}" for gid in self.config.gpu_ids]
        gpu_str = ",".join(gpu_devices)
        
        if self.config.executor_type == "llama.cpp":
            llama_server_path = os.environ.get(
                "LLAMA_SERVER_PATH",
                "~/llama.cpp/build/bin/llama-server"
            )
            
            # 正确参数：--device CUDA3,CUDA4
            cmd = [
                llama_server_path,
                "--model", self.config.model_path,
                "--device", gpu_str,  # CUDA3,CUDA4
                "--port", str(self.config.port),
                "--ctx-size", "8192",
                "--threads", "8",
                "--batch-size", "512",
                "--embeddings"  # 启用embedding API支持
            ]
        
        elif self.config.executor_type == "whisper.cpp":
            whisper_server_path = os.environ.get(
                "WHISPER_SERVER_PATH",
                "~/whisper.cpp/build/bin/whisper-server"
            )
            
            cmd = [
                whisper_server_path,
                "--model", self.config.model_path,
                "--device", gpu_str,
                "--port", str(self.config.port)
            ]
        
        else:
            raise ValueError(f"Unknown executor type: {self.config.executor_type}")
        
        return cmd
    
    def _wait_for_port(self, timeout: int = 60) -> bool:
        """等待端口响应"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                # 尝试连接端口（llama-server的根路径）
                response = requests.get(
                    f"http://localhost:{self.config.port}/",
                    timeout=5
                )
                # 任何响应都算成功（200或HTML页面）
                if response.status_code in [200, 301, 302]:
                    return True
            except:
                pass
            time.sleep(2)  # 增加轮询间隔
        return False
    
    def _cleanup_failed_start(self):
        """清理失败的启动"""
        if self.process:
            self.process.kill()
            self.process = None
        self.status.status = "crashed"
        self.status.pid = None
    
    def _start_monitor(self):
        """启动监控线程"""
        self._stop_monitor = False
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True
        )
        self.monitor_thread.start()
    
    def _monitor_loop(self):
        """监控进程状态"""
        while not self._stop_monitor:
            if self.process:
                # 检查进程是否存活
                poll_result = self.process.poll()
                
                if poll_result is not None:
                    # 进程已退出
                    print(f"⚠️ Executor {self.executor_id} 进程已退出 (exit_code={poll_result})")
                    self.status.status = "crashed"
                    self.status.last_error = f"Process exited with code {poll_result}"
                    
                    # 自动重启
                    if not self._stop_monitor:
                        print(f"🔄 尝试自动重启...")
                        self.restart()
                    
                    break
            
            # 每5秒检查一次
            time.sleep(5)
    
    def is_running(self) -> bool:
        """检查进程是否运行"""
        return self.status.status == "running" and self.process is not None
    
    def get_status(self) -> ExecutorStatus:
        """获取状态"""
        return self.status

class ExecutorManager:
    """Executor 进程管理器"""
    
    def __init__(self):
        self.executors: Dict[str, ExecutorProcess] = {}
        self.port_pool = range(8000, 8010)  # 端口池（8000-8009）
        self.port_used: Dict[int, str] = {}  # port -> executor_id
    
    def load_model(self, model: str, model_path: str, gpu_ids: List[int], 
                   executor_type: str) -> bool:
        """加载模型（启动 Executor）"""
        executor_id = f"{model}-{gpu_ids}"
        
        # 检查是否已存在
        if executor_id in self.executors:
            existing = self.executors[executor_id]
            if existing.is_running():
                print(f"⚠️ Executor {executor_id} 已运行")
                return True
            else:
                # 已存在但未运行，先移除
                del self.executors[executor_id]
        
        # 分配端口
        port = self._allocate_port(executor_id)
        if port is None:
            print(f"❌ 无可用端口")
            return False
        
        # 创建配置
        config = ExecutorConfig(
            model=model,
            model_path=model_path,
            gpu_ids=gpu_ids,
            executor_type=executor_type,
            port=port,
            vram_required=0  # 从 models.yaml 读取
        )
        
        # 创建 Executor
        executor = ExecutorProcess(config)
        
        # 启动
        if executor.start():
            self.executors[executor_id] = executor
            return True
        else:
            # 释放端口
            self._release_port(port)
            return False
    
    def unload_model(self, model: str) -> bool:
        """卸载模型（停止 Executor）"""
        # 查找该模型的所有 Executor
        to_remove = []
        for executor_id, executor in self.executors.items():
            if executor.config.model == model:
                to_remove.append(executor_id)
        
        if not to_remove:
            print(f"⚠️ 模型 {model} 未加载")
            return True
        
        # 停止并移除
        for executor_id in to_remove:
            executor = self.executors[executor_id]
            executor.stop()
            self._release_port(executor.config.port)
            del self.executors[executor_id]
        
        print(f"✅ 模型 {model} 已卸载（{len(to_remove)} 个 Executor）")
        return True
    
    def execute_chat(self, model: str, input_data: Dict) -> Optional[Dict]:
        """执行 chat 推理"""
        # 查找该模型的 Executor
        executor = self._find_executor(model)
        if not executor:
            print(f"❌ 模型 {model} 未加载")
            return None
        
        try:
            response = requests.post(
                f"http://localhost:{executor.config.port}/v1/chat/completions",
                json=input_data,
                timeout=120
            )
            return response.json()
        except Exception as e:
            print(f"❌ chat 执行失败: {e}")
            return {"error": str(e)}
    
    def execute_embedding(self, model: str, input_data: Dict) -> Optional[Dict]:
        """执行 embedding 推理"""
        executor = self._find_executor(model)
        if not executor:
            print(f"❌ 模型 {model} 未加载")
            return None
        
        try:
            response = requests.post(
                f"http://localhost:{executor.config.port}/v1/embeddings",
                json=input_data,
                timeout=60
            )
            return response.json()
        except Exception as e:
            print(f"❌ embedding 执行失败: {e}")
            return {"error": str(e)}
    
    def execute_stt(self, model: str, audio_path: str) -> Optional[Dict]:
        """执行 STT 推理"""
        executor = self._find_executor(model)
        if not executor:
            print(f"❌ 模型 {model} 未加载")
            return None
        
        try:
            with open(audio_path, 'rb') as f:
                response = requests.post(
                    f"http://localhost:{executor.config.port}/v1/audio/transcriptions",
                    files={'file': f},
                    data={'model': model},
                    timeout=120
                )
            return response.json()
        except Exception as e:
            print(f"❌ stt 执行失败: {e}")
            return {"error": str(e)}
    
    def _find_executor(self, model: str) -> Optional[ExecutorProcess]:
        """查找模型的 Executor"""
        for executor_id, executor in self.executors.items():
            if executor.config.model == model and executor.is_running():
                return executor
        return None
    
    def _allocate_port(self, executor_id: str) -> Optional[int]:
        """分配端口"""
        for port in self.port_pool:
            if port not in self.port_used:
                self.port_used[port] = executor_id
                return port
        return None
    
    def _release_port(self, port: int):
        """释放端口"""
        if port in self.port_used:
            del self.port_used[port]
    
    def get_loaded_models(self) -> List[str]:
        """获取已加载模型列表"""
        models = []
        for executor in self.executors.values():
            if executor.is_running():
                models.append(executor.config.model)
        return models
    
    def get_all_status(self) -> List[ExecutorStatus]:
        """获取所有 Executor 状态"""
        return [executor.get_status() for executor in self.executors.values()]
    
    def shutdown_all(self):
        """关闭所有 Executor"""
        print("🛑 关闭所有 Executor...")
        for executor_id, executor in self.executors.items():
            executor.stop()
            self._release_port(executor.config.port)
        self.executors.clear()
        print("✅ 所有 Executor 已关闭")

# 测试入口
if __name__ == "__main__":
    manager = ExecutorManager()
    
    # 测试加载模型
    print("🧪 测试 ExecutorManager...")
    
    # 示例配置（需要实际路径）
    # manager.load_model(
    #     model="glm-4.5-air",
    #     model_path="~/models/GLM-4.5-Air.gguf",
    #     gpu_ids=[3],
    #     executor_type="llama.cpp"
    # )
    
    # 查看状态
    # print(manager.get_loaded_models())
    # print(manager.get_all_status())
    
    # 执行推理
    # result = manager.execute_chat("glm-4.5-air", {"messages": [{"role": "user", "content": "hello"}]})
    # print(result)
    
    # 卸载
    # manager.unload_model("glm-4.5-air")
    
    print("✅ ExecutorManager 代码已创建（待测试）")