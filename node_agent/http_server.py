#!/usr/bin/env python3
"""
Node Agent HTTP Server - 监听8001端口

职责：
- 接收总控端指令（通过SSH隧道）
- 端点：/load_model, /unload_model, /execute_task, /get_status
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
import uvicorn

# 导入 ExecutorManager
import sys
sys.path.append("/home/openclawvm/.openclaw/workspace/gpuhub/node_agent")
from executor_manager import ExecutorManager

# 创建 FastAPI 应用
app = FastAPI(title="GPUHub Node Agent", version="2.0")

# ExecutorManager 实例
executor_manager = ExecutorManager()

# ============== 请求模型 ==============

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
    task_type: str  # "chat", "embedding", "stt"
    input: Dict[str, Any]

class GetStatusResponse(BaseModel):
    loaded_models: List[str]
    executors: List[Dict]

# ============== 端点定义 ==============

@app.get("/")
def root():
    """根端点（健康检查）"""
    return {"status": "ok", "service": "GPUHub Node Agent v2"}

@app.post("/load_model")
def load_model(request: LoadModelRequest):
    """加载模型（启动Executor）"""
    success = executor_manager.load_model(
        model=request.model,
        model_path=request.model_path,
        gpu_ids=request.gpu_ids,
        executor_type=request.executor_type
    )
    
    if success:
        return {"status": "success", "model": request.model, "gpu_ids": request.gpu_ids}
    else:
        raise HTTPException(status_code=500, detail="Failed to load model")

@app.post("/unload_model")
def unload_model(request: UnloadModelRequest):
    """卸载模型（停止Executor）"""
    success = executor_manager.unload_model(request.model)
    
    if success:
        return {"status": "success", "model": request.model}
    else:
        raise HTTPException(status_code=500, detail="Failed to unload model")

@app.post("/execute_task")
def execute_task(request: ExecuteTaskRequest):
    """执行任务"""
    result = None
    
    if request.task_type == "chat":
        result = executor_manager.execute_chat(request.model, request.input)
    elif request.task_type == "embedding":
        result = executor_manager.execute_embedding(request.model, request.input)
    elif request.task_type == "stt":
        audio_path = request.input.get("audio_path")
        result = executor_manager.execute_stt(request.model, audio_path)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown task type: {request.task_type}")
    
    if result is None:
        raise HTTPException(status_code=500, detail="Execution failed (model not loaded?)")
    
    return {"status": "success", "task_id": request.task_id, "result": result}

@app.get("/get_status")
def get_status():
    """获取节点状态"""
    loaded_models = executor_manager.get_loaded_models()
    executors = [
        {
            "executor_id": status.executor_id,
            "model": status.config.model,
            "gpu_ids": status.config.gpu_ids,
            "port": status.port,
            "status": status.status,
            "pid": status.pid,
            "restart_count": status.restart_count
        }
        for status in executor_manager.get_all_status()
    ]
    
    return GetStatusResponse(loaded_models=loaded_models, executors=executors)

# ============== 启动入口 ==============

if __name__ == "__main__":
    print("🚀 Node Agent HTTP Server 启动...")
    print("📍 监听端口: 8001")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        log_level="info"
    )