#!/usr/bin/env python3
"""
GPUHub Node Agent - Worker Node

算力端代理：
- 心跳上报（GPU 状态）
- 任务拉取与执行
- 结果返回
"""

import os
import json
import time
import subprocess
import requests
from datetime import datetime
from typing import Optional, Dict, Any

# Control Plane 地址（从环境变量读取，无默认值）
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL")
if not CONTROL_PLANE_URL:
    raise ValueError("CONTROL_PLANE_URL 环境变量未设置")

# 节点 ID
NODE_ID = os.environ.get("NODE_ID", "hccs86-01")

# 心跳间隔（秒）
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", 10))

# 任务拉取间隔（秒）
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL", 5))

class NodeAgent:
    def __init__(self):
        self.node_id = NODE_ID
        self.control_plane_url = CONTROL_PLANE_URL
        self.running = True
        self.running_tasks = {}  # request_id -> task_info
    
    def get_gpu_status(self):
        """获取 GPU 状态（nvidia-smi）"""
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
                        "process_count": 0  # 简化
                    })
            
            return gpu_status
        except Exception as e:
            print(f"❌ 获取 GPU 状态失败: {e}")
            return []
    
    def heartbeat(self):
        """发送心跳到 Control Plane"""
        gpu_status = self.get_gpu_status()
        
        payload = {
            "node_id": self.node_id,
            "timestamp": datetime.utcnow().isoformat(),
            "gpu_status": gpu_status,
            "task_status": {
                "running_tasks": list(self.running_tasks.keys()),
                "completed_tasks": 0,  # 简化
                "failed_tasks": 0
            },
            "supported_tasks": ["chat", "embedding", "stt"]
        }
        
        try:
            response = requests.post(
                f"{self.control_plane_url}/heartbeat",
                json=payload,
                timeout=10
            )
            
            if response.status_code == 200:
                print(f"✅ 心跳上报成功: {len(gpu_status)} GPUs")
            else:
                print(f"⚠️ 心跳上报失败: {response.status_code}")
        except Exception as e:
            print(f"❌ 心跳上报异常: {e}")
    
    def fetch_task(self):
        """从 Control Plane 拉取任务"""
        gpu_status = self.get_gpu_status()
        
        # 筛选可用 GPU（显存占用 < 46GB，即至少有少量空闲）
        available_gpus = []
        available_memory = []
        for gpu in gpu_status:
            # 可用显存 = 总显存 - 已用显存
            available = gpu["memory_total"] - gpu["memory_used"]
            # 只要有 >2GB 空闲就认为可用（简化逻辑）
            if available > 2 * 1024:  # 2GB
                available_gpus.append(gpu["gpu_id"])
                available_memory.append(available)
        
        if not available_gpus:
            print("⏳ 无可用 GPU，不拉取任务")
            return None
        
        payload = {
            "node_id": self.node_id,
            "available_gpus": available_gpus,
            "available_memory": available_memory
        }
        
        try:
            response = requests.post(
                f"{self.control_plane_url}/fetch_task",
                json=payload,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("task"):
                    print(f"✅ 任务已拉取: {data['task']['request_id']}")
                    return data["task"]
                else:
                    print("⏳ 队列无任务")
                    return None
            else:
                print(f"⚠️ 任务拉取失败: {response.status_code}")
                return None
        except Exception as e:
            print(f"❌ 任务拉取异常: {e}")
            return None
    
    def execute_task(self, task: Dict[str, Any]):
        """执行任务（调用 llama/whisper）"""
        request_id = task["request_id"]
        task_type = task["task_type"]
        gpu_id = task.get("selected_gpu_id", 0)
        
        # 从 Control Plane 获取完整请求信息
        try:
            response = requests.get(
                f"{self.control_plane_url}/dashboard/request/{request_id}",
                timeout=10
            )
            
            if response.status_code != 200:
                print(f"❌ 无法获取请求信息: {request_id}")
                return
            
            request_data = response.json()["request"]
            input_ref = json.loads(request_data["input_ref"])
        except Exception as e:
            print(f"❌ 获取请求信息异常: {e}")
            return
        
        # 标记任务开始
        start_time = datetime.utcnow()
        self.running_tasks[request_id] = task
        
        print(f"🚀 开始执行任务: {request_id} (type={task_type}, gpu={gpu_id})")
        
        # 调用执行器（简化：直接调用 llama-guardian）
        try:
            if task_type == "chat":
                result = self.execute_chat(input_ref, gpu_id)
            elif task_type == "embedding":
                result = self.execute_embedding(input_ref, gpu_id)
            elif task_type == "stt":
                result = self.execute_stt(input_ref, gpu_id)
            else:
                raise ValueError(f"Unknown task type: {task_type}")
            
            run_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            
            # 上报成功
            self.report_result(request_id, "succeeded", result, run_ms)
            print(f"✅ 任务完成: {request_id} (run_ms={run_ms})")
            
        except Exception as e:
            run_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            
            # 上报失败
            self.report_result(request_id, "failed", None, run_ms, "EXECUTION_ERROR", str(e))
            print(f"❌ 任务失败: {request_id} ({e})")
        
        # 移除任务
        del self.running_tasks[request_id]
    
    def execute_chat(self, input_ref: Dict[str, Any], gpu_id: int):
        """执行 chat 任务"""
        # 调用 llama-guardian（简化）
        # TODO: 实现实际调用
        
        # 模拟返回
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Mock response"},
                    "finish_reason": "stop"
                }
            ]
        }
    
    def execute_embedding(self, input_ref: Dict[str, Any], gpu_id: int):
        """执行 embedding 任务"""
        # TODO: 实现实际调用
        
        return {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": [0.1, 0.2, 0.3]  # Mock
                }
            ]
        }
    
    def execute_stt(self, input_ref: Dict[str, Any], gpu_id: int):
        """执行 stt 任务"""
        audio_path = input_ref.get("audio_path")
        model = input_ref.get("model", "whisper-large-v3")
        
        # TODO: 实现实际调用
        
        return {
            "text": "Mock transcription"
        }
    
    def report_result(self, request_id: str, status: str, result: Optional[Dict], 
                      run_ms: int, error_code: Optional[str] = None, 
                      error_message: Optional[str] = None):
        """上报任务结果"""
        payload = {
            "request_id": request_id,
            "node_id": self.node_id,
            "status": status,
            "result": result,
            "run_ms": run_ms,
            "error_code": error_code,
            "error_message": error_message
        }
        
        try:
            response = requests.post(
                f"{self.control_plane_url}/task_result",
                json=payload,
                timeout=10
            )
            
            if response.status_code == 200:
                print(f"✅ 结果上报成功: {request_id}")
            else:
                print(f"⚠️ 结果上报失败: {response.status_code}")
        except Exception as e:
            print(f"❌ 结果上报异常: {e}")
    
    def run(self):
        """主循环"""
        print(f"🚀 Node Agent 启动: {self.node_id}")
        print(f"📍 Control Plane: {self.control_plane_url}")
        
        last_heartbeat = 0
        last_fetch = 0
        
        while self.running:
            now = time.time()
            
            # 心跳上报
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                self.heartbeat()
                last_heartbeat = now
            
            # 任务拉取
            if now - last_fetch >= FETCH_INTERVAL:
                task = self.fetch_task()
                if task:
                    self.execute_task(task)
                last_fetch = now
            
            time.sleep(1)

if __name__ == "__main__":
    agent = NodeAgent()
    agent.run()