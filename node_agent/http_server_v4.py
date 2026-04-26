#!/usr/bin/env python3
"""
Node Agent HTTP Server v4 - 支持自动任务拉取

新增功能：
- 后台线程持续从 Control Plane 拉取任务
- 任务执行并上报结果
- 支持模型列表上报

职责：
- 接收总控端指令（HTTP）
- 自动拉取队列任务
- 端点：/load_model, /unload_model, /execute_task, /get_status
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
import uvicorn
import yaml
import os
import subprocess
import threading
import requests
import json
import time
from datetime import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from executor_manager import ExecutorManager

# ============== 配置 ==============

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://192.168.1.6:8003")
NODE_ID = os.environ.get("NODE_ID", "hccs86-01")
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL", "5"))  # 秒
MODELS_CONFIG_PATH = os.path.expanduser("~") + "/gpuhub/node_agent_v2/models.yaml"

# ============== FastAPI App ==============

app = FastAPI(title="GPUHub Node Agent", version="4.0")

executor_manager = ExecutorManager()

# 任务拉取线程控制
_stop_fetch_thread = False
_fetch_thread = None

# ============== 辅助函数 ==============

def get_gpu_status():
    """Return GPU memory status for scheduler decisions."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        gpus = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            idx, used, total = [part.strip() for part in line.split(',')]
            used_i = int(used)
            total_i = int(total)
            gpus.append({
                "gpu_id": int(idx),
                "memory_used": used_i,
                "memory_total": total_i,
                "memory_free": total_i - used_i,
            })
        return gpus
    except Exception as e:
        print(f"获取GPU状态失败: {e}")
        return []


def load_models_config():
    try:
        with open(MODELS_CONFIG_PATH, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"加载 models.yaml 失败: {e}")
        return {}


# ============== 任务拉取循环 ==============

def fetch_and_execute_loop():
    """后台线程：持续从 Control Plane 拉取任务并执行"""
    global _stop_fetch_thread
    
    print(f"🚀 任务拉取线程启动 (Control Plane: {CONTROL_PLANE_URL})")
    
    while not _stop_fetch_thread:
        try:
            # 获取可用 GPU
            gpu_status = get_gpu_status()
            available_gpus = []
            available_memory = []
            
            for gpu in gpu_status:
                # 只要有 >2GB 空闲就认为可用
                if gpu["memory_free"] > 2 * 1024:
                    available_gpus.append(gpu["gpu_id"])
                    available_memory.append(gpu["memory_free"])
            
            if not available_gpus:
                print("⏳ 无可用 GPU，跳过任务拉取")
                time.sleep(FETCH_INTERVAL)
                continue
            
            # 从 Control Plane 拉取任务
            payload = {
                "node_id": NODE_ID,
                "available_gpus": available_gpus,
                "available_memory": available_memory
            }
            
            response = requests.post(
                f"{CONTROL_PLANE_URL}/fetch_task",
                json=payload,
                timeout=10
            )
            
            if response.status_code != 200:
                print(f"⚠️ 任务拉取失败: {response.status_code}")
                time.sleep(FETCH_INTERVAL)
                continue
            
            data = response.json()
            task = data.get("task")
            
            if not task:
                # 无任务，静默等待
                time.sleep(FETCH_INTERVAL)
                continue
            
            print(f"✅ 任务已拉取: {task['request_id']}")
            
            # 执行任务
            execute_task_from_queue(task)
            
        except Exception as e:
            print(f"❌ 任务拉取循环异常: {e}")
            time.sleep(FETCH_INTERVAL)
    
    print("🛑 任务拉取线程已停止")


def execute_task_from_queue(task: Dict[str, Any]):
    """执行从队列拉取的任务"""
    request_id = task["request_id"]
    task_type = task["task_type"]
    selected_gpu_id = task.get("selected_gpu_id", 0)
    
    start_time = datetime.utcnow()
    
    try:
        # 从 Control Plane 获取完整请求信息
        response = requests.get(
            f"{CONTROL_PLANE_URL}/dashboard/request/{request_id}",
            timeout=10
        )
        
        if response.status_code != 200:
            print(f"❌ 无法获取请求信息: {request_id}")
            report_result(request_id, "failed", None, 0, "FETCH_ERROR", "Cannot fetch request details")
            return
        
        request_data = response.json()["request"]
        input_ref = json.loads(request_data["input_ref"])
        model = input_ref.get("model", "glm-4.5-air")
        
        print(f"🚀 执行任务: {request_id} (type={task_type}, model={model})")
        
        # 执行
        if task_type == "chat":
            result = executor_manager.execute_chat(model, input_ref)
        elif task_type == "embedding":
            result = executor_manager.execute_embedding(model, input_ref)
        elif task_type == "stt":
            audio_path = input_ref.get("audio_path")
            result = executor_manager.execute_stt(model, audio_path)
        else:
            raise ValueError(f"Unknown task type: {task_type}")
        
        run_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        
        if result and not result.get("error"):
            report_result(request_id, "succeeded", result, run_ms)
            print(f"✅ 任务完成: {request_id} (run_ms={run_ms})")
        else:
            error_msg = result.get("error", "Unknown error") if result else "Execution failed"
            report_result(request_id, "failed", None, run_ms, "EXECUTION_ERROR", str(error_msg))
            print(f"❌ 任务失败: {request_id} ({error_msg})")
        
    except Exception as e:
        run_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        report_result(request_id, "failed", None, run_ms, "EXECUTION_ERROR", str(e))
        print(f"❌ 任务执行异常: {request_id} ({e})")


def report_result(request_id: str, status: str, result: Optional[Dict], 
                  run_ms: int, error_code: Optional[str] = None,
                  error_message: Optional[str] = None):
    """上报任务结果到 Control Plane"""
    payload = {
        "request_id": request_id,
        "node_id": NODE_ID,
        "status": status,
        "result": result,
        "run_ms": run_ms,
        "error_code": error_code,
        "error_message": error_message
    }
    
    try:
        response = requests.post(
            f"{CONTROL_PLANE_URL}/task_result",
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"✅ 结果上报成功: {request_id}")
        else:
            print(f"⚠️ 结果上报失败: {response.status_code}")
    except Exception as e:
        print(f"❌ 结果上报异常: {e}")


# ============== Pydantic 模型 ==============

class LoadModelRequest(BaseModel):
    model: str
    model_path: str
    gpu_ids: List[int]
    executor_type: str = "llama.cpp"

class UnloadModelRequest(BaseModel):
    model: str

class ExecuteTaskRequest(BaseModel):
    task_id: str
    model: str
    task_type: str
    input: Dict[str, Any]

# ============== 端点定义 ==============

@app.get("/")
def root():
    return {"status": "ok", "service": "GPUHub Node Agent v4"}

@app.on_event("startup")
def startup_event():
    """启动时启动任务拉取线程"""
    global _fetch_thread, _stop_fetch_thread
    
    _stop_fetch_thread = False
    _fetch_thread = threading.Thread(target=fetch_and_execute_loop, daemon=True)
    _fetch_thread.start()
    print("✅ 任务拉取线程已启动")


@app.on_event("shutdown")
def shutdown_event():
    """停止时停止任务拉取线程"""
    global _stop_fetch_thread
    _stop_fetch_thread = True
    print("🛑 任务拉取线程停止信号已发送")


@app.post("/load_model")
def load_model(request: LoadModelRequest):
    success = executor_manager.load_model(
        model=request.model,
        model_path=request.model_path,
        gpu_ids=request.gpu_ids,
        executor_type=request.executor_type
    )
    if success:
        return {"status": "success", "model": request.model}
    else:
        raise HTTPException(500, "Failed to load model")

@app.post("/unload_model")
def unload_model(request: UnloadModelRequest):
    success = executor_manager.unload_model(request.model)
    if success:
        return {"status": "success"}
    else:
        raise HTTPException(500, "Failed to unload model")

@app.post("/execute_task")
def execute_task(request: ExecuteTaskRequest):
    """直接执行任务（不经过队列）"""
    if request.task_type == "chat":
        result = executor_manager.execute_chat(request.model, request.input)
    elif request.task_type == "embedding":
        result = executor_manager.execute_embedding(request.model, request.input)
    elif request.task_type == "stt":
        result = executor_manager.execute_stt(request.model, request.input.get("audio_path"))
    else:
        raise HTTPException(400, f"Unknown task type: {request.task_type}")
    
    return {"status": "success", "result": result}

@app.get("/get_status")
def get_status():
    """获取节点状态 - 包含可用模型列表"""
    config = load_models_config()
    
    loaded_models = executor_manager.get_loaded_models()
    
    available_models = []
    if config and 'models' in config:
        for model_name, model_info in config['models'].items():
            available_models.append({
                "id": model_name,
                "vram_required": model_info.get('vram_required', 0),
                "executor": model_info.get('executor', 'llama.cpp'),
                "loaded": model_name in loaded_models
            })
    
    executors = [
        {
            "executor_id": status.executor_id,
            "model": status.config.model,
            "gpu_ids": status.config.gpu_ids,
            "port": status.port,
            "status": status.status,
            "pid": status.pid
        }
        for status in executor_manager.get_all_status()
    ]
    
    return {
        "node_id": config.get('node_id', 'unknown'),
        "loaded_models": loaded_models,
        "available_models": available_models,
        "executors": executors,
        "gpu_status": get_gpu_status(),
        "control_plane_url": CONTROL_PLANE_URL,
        "fetch_thread_running": _fetch_thread is not None and _fetch_thread.is_alive(),
    }

@app.get("/queue_status")
def queue_status():
    """查看 Control Plane 队列状态"""
    try:
        response = requests.get(
            f"{CONTROL_PLANE_URL}/dashboard/queues",
            timeout=10
        )
        return response.json()
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    print("🚀 Node Agent v4 启动...")
    print(f"📍 监听端口: 8001")
    print(f"📍 Control Plane: {CONTROL_PLANE_URL}")
    print(f"📍 节点ID: {NODE_ID}")
    
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")