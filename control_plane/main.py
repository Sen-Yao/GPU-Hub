#!/usr/bin/env python3
"""
GPUHub Control Plane - FastAPI Main Entry

总控端核心服务：
- REST API 端点（chat / embedding / stt）
- Redis 队列管理
- MySQL 请求跟踪
- 节点心跳接收
"""

import os
import uuid
import json
from datetime import datetime
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis
import mysql.connector
from typing import List, Optional, Dict, Any

# 初始化 FastAPI
app = FastAPI(
    title="GPUHub Control Plane",
    description="GPU 任务调度平台 - 总控端",
    version="1.0.0"
)

# CORS（前端访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis 连接（从环境变量读取，无默认值）
REDIS_HOST = os.environ.get("REDIS_HOST")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")

if not REDIS_HOST:
    raise ValueError("REDIS_HOST 环境变量未设置")

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    decode_responses=True
)

# MySQL 连接（从环境变量读取，无默认值）
MYSQL_HOST = os.environ.get("MYSQL_HOST")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "gpuhub")

if not MYSQL_HOST or not MYSQL_PASSWORD:
    raise ValueError("MYSQL_HOST 或 MYSQL_PASSWORD 环境变量未设置")

def get_mysql_connection():
    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE
    )

# Pydantic 模型
class ChatRequest(BaseModel):
    model: str
    messages: List[Dict[str, str]]
    temperature: float = 0.7
    max_tokens: int = 2048

class EmbeddingRequest(BaseModel):
    model: str
    input: str

class HeartbeatRequest(BaseModel):
    node_id: str
    timestamp: str
    gpu_status: List[Dict[str, Any]]
    task_status: Dict[str, Any]
    supported_tasks: List[str]

class FetchTaskRequest(BaseModel):
    node_id: str
    available_gpus: List[int]
    available_memory: List[int]

class TaskResultRequest(BaseModel):
    request_id: str
    node_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    run_ms: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

# 节点状态缓存（内存 + Redis）
nodes_status = {}

# ==================== API 端点 ====================

@app.get("/")
async def root():
    return {"message": "GPUHub Control Plane v1.0"}

@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "healthy",
        "redis": redis_client.ping(),
        "mysql": True
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    """Chat API 端点"""
    request_id = str(uuid.uuid4())
    created_at = datetime.utcnow()
    
    # 存入 MySQL
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO requests (request_id, user_id, task_type, status, input_ref, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (request_id, "senyao", "chat", "received", json.dumps(request.dict()), created_at)
    )
    conn.commit()
    conn.close()
    
    # 加入 Redis 队列
    queue_item = {
        "request_id": request_id,
        "task_type": "chat",
        "priority": 1,
        "created_at": created_at.isoformat()
    }
    redis_client.lpush("gpuhub:queue", json.dumps(queue_item))
    
    return {
        "request_id": request_id,
        "status": "queued",
        "message": "请求已加入队列"
    }

@app.post("/v1/embeddings")
async def embeddings(request: EmbeddingRequest):
    """Embedding API 端点"""
    request_id = str(uuid.uuid4())
    created_at = datetime.utcnow()
    
    # 存入 MySQL
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO requests (request_id, user_id, task_type, status, input_ref, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (request_id, "senyao", "embedding", "received", json.dumps(request.dict()), created_at)
    )
    conn.commit()
    conn.close()
    
    # 加入 Redis 队列
    queue_item = {
        "request_id": request_id,
        "task_type": "embedding",
        "priority": 1,
        "created_at": created_at.isoformat()
    }
    redis_client.lpush("gpuhub:queue", json.dumps(queue_item))
    
    return {
        "request_id": request_id,
        "status": "queued",
        "message": "请求已加入队列"
    }

@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(...)
):
    """STT API 端点"""
    request_id = str(uuid.uuid4())
    created_at = datetime.utcnow()
    
    # 读取音频文件（临时存储）
    audio_data = await file.read()
    audio_path = f"/tmp/{request_id}.{file.filename.split('.')[-1]}"
    with open(audio_path, "wb") as f:
        f.write(audio_data)
    
    # 存入 MySQL
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO requests (request_id, user_id, task_type, status, input_ref, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (request_id, "senyao", "stt", "received", json.dumps({"audio_path": audio_path, "model": model}), created_at)
    )
    conn.commit()
    conn.close()
    
    # 加入 Redis 队列
    queue_item = {
        "request_id": request_id,
        "task_type": "stt",
        "priority": 1,
        "created_at": created_at.isoformat()
    }
    redis_client.lpush("gpuhub:queue", json.dumps(queue_item))
    
    return {
        "request_id": request_id,
        "status": "queued",
        "message": "请求已加入队列"
    }

# ==================== 心跳与任务分发 ====================

@app.post("/heartbeat")
async def heartbeat(request: HeartbeatRequest):
    """接收 Node Agent 心跳"""
    node_id = request.node_id
    
    # 更新节点状态缓存
    nodes_status[node_id] = {
        "last_heartbeat": request.timestamp,
        "gpu_status": request.gpu_status,
        "task_status": request.task_status,
        "supported_tasks": request.supported_tasks
    }
    
    # 存入 Redis（持久化）
    redis_client.set(f"gpuhub:node:{node_id}", json.dumps(nodes_status[node_id]), ex=120)
    
    return {
        "acknowledged": True,
        "assigned_tasks": [],
        "commands": []
    }

@app.post("/fetch_task")
async def fetch_task(request: FetchTaskRequest):
    """Node Agent 拉取任务"""
    # 从队列取出任务
    queue_item = redis_client.rpop("gpuhub:queue")
    
    if not queue_item:
        return {"task": None}
    
    task_data = json.loads(queue_item)
    
    # 选择 GPU（简化：选第一个可用）
    selected_gpu_id = request.available_gpus[0]
    
    # 更新请求状态为 scheduled
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE requests SET status = %s, selected_node = %s, selected_gpu_ids = %s, updated_at = %s
        WHERE request_id = %s
        """,
        ("scheduled", request.node_id, str(selected_gpu_id), datetime.utcnow(), task_data["request_id"])
    )
    conn.commit()
    conn.close()
    
    return {
        "task": {
            "request_id": task_data["request_id"],
            "task_type": task_data["task_type"],
            "selected_gpu_id": selected_gpu_id
        }
    }

@app.post("/task_result")
async def task_result(request: TaskResultRequest):
    """接收 Node Agent 任务结果"""
    # 更新请求状态
    conn = get_mysql_connection()
    cursor = conn.cursor()
    
    status = request.status
    output_ref = json.dumps(request.result) if request.result else None
    error_code = request.error_code
    error_message = request.error_message
    run_ms = request.run_ms
    
    cursor.execute(
        """
        UPDATE requests 
        SET status = %s, output_ref = %s, error_code = %s, error_message = %s, run_ms = %s, updated_at = %s
        WHERE request_id = %s
        """,
        (status, output_ref, error_code, error_message, run_ms, datetime.utcnow(), request.request_id)
    )
    
    # 记录状态历史
    cursor.execute(
        """
        INSERT INTO request_history (request_id, from_status, to_status, timestamp, message)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (request.request_id, "running", status, datetime.utcnow(), f"Node {request.node_id} reported")
    )
    
    conn.commit()
    conn.close()
    
    return {"acknowledged": True}

# ==================== 前端仪表盘 ====================

@app.get("/dashboard/requests")
async def dashboard_requests(page: int = 1, limit: int = 20):
    """请求列表"""
    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)
    
    offset = (page - 1) * limit
    cursor.execute(
        """
        SELECT request_id, task_type, status, created_at, selected_node
        FROM requests
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """,
        (limit, offset)
    )
    requests = cursor.fetchall()
    
    cursor.execute("SELECT COUNT(*) as total FROM requests")
    total = cursor.fetchone()["total"]
    
    conn.close()
    
    return {
        "requests": requests,
        "total": total,
        "page": page
    }

@app.get("/dashboard/nodes")
async def dashboard_nodes():
    """节点状态"""
    node_keys = redis_client.keys("gpuhub:node:*")
    nodes = []
    
    for key in node_keys:
        node_data = redis_client.get(key)
        if node_data:
            nodes.append(json.loads(node_data))
    
    return {"nodes": nodes}

@app.get("/dashboard/queues")
async def dashboard_queues():
    """队列状态"""
    queue_length = redis_client.llen("gpuhub:queue")
    queue_items = redis_client.lrange("gpuhub:queue", 0, 10)
    
    tasks = [json.loads(item) for item in queue_items]
    
    return {
        "queue_length": queue_length,
        "tasks": tasks
    }

@app.get("/dashboard/request/{request_id}")
async def dashboard_request_detail(request_id: str):
    """单个请求详情"""
    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute(
        """
        SELECT * FROM requests WHERE request_id = %s
        """,
        (request_id,)
    )
    request = cursor.fetchone()
    
    if not request:
        raise HTTPException(status_code=404, detail="Request not found")
    
    cursor.execute(
        """
        SELECT * FROM request_history WHERE request_id = %s
        ORDER BY timestamp ASC
        """,
        (request_id,)
    )
    history = cursor.fetchall()
    
    conn.close()
    
    return {
        "request": request,
        "history": history
    }

# ==================== 启动 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)