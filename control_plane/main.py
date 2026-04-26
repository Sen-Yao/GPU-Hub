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
import time
from datetime import datetime
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
import redis
import mysql.connector
from typing import List, Optional, Dict, Any, Union

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
class ChatMessage(BaseModel):
    """OpenAI-compatible chat message.

    Keep this intentionally permissive: gateways such as AxonHub may forward
    multimodal content blocks, tool calls, tool responses, or provider-specific
    fields. Narrow schemas cause FastAPI/Pydantic to reject requests with 422
    before the endpoint can enqueue the task.
    """

    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ChatRequest(BaseModel):
    """OpenAI-compatible /v1/chat/completions request.

    GPUHub only needs a subset for queueing, but the ingress schema must accept
    the broader OpenAI payload shape so API gateways can use GPUHub as a
    provider without tripping validation.
    """

    model_config = ConfigDict(extra="allow")

    model: str = "glm-4.5-air"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 2048
    top_p: Optional[float] = None
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "bge-m3"
    input: Union[str, List[str]]

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

# ==================== Validation logging ====================

SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key"}


def _safe_headers(headers) -> Dict[str, str]:
    return {
        key: ("<redacted>" if key.lower() in SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Log enough context to debug OpenAI-compatible gateway payload issues."""
    body = await request.body()
    body_text = body.decode("utf-8", errors="replace")[:4000]
    print(
        "[VALIDATION ERROR] "
        f"path={request.url.path} errors={exc.errors()} "
        f"headers={_safe_headers(request.headers)} body={body_text}"
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ==================== 认证 ====================

import hashlib
import secrets

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    print("⚠️ ADMIN_PASSWORD 环境变量未设置，管理员登录将不可用")

# 简单的 token 存储（生产环境应使用 JWT + Redis）
active_tokens = set()

def verify_token(token: str) -> bool:
    return token in active_tokens

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

@app.post("/auth/login")
async def login(request: dict):
    """管理员登录"""
    password = request.get("password", "")
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Admin login not configured")
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")
    token = secrets.token_hex(32)
    active_tokens.add(token)
    return {"token": token, "message": "Login successful"}

@app.post("/auth/verify")
async def verify(request: dict):
    """验证 token"""
    token = request.get("token", "")
    if not verify_token(token):
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"valid": True}

@app.post("/auth/logout")
async def logout(request: dict):
    """登出"""
    token = request.get("token", "")
    active_tokens.discard(token)
    return {"message": "Logged out"}

CHAT_COMPLETION_WAIT_TIMEOUT = float(os.environ.get("CHAT_COMPLETION_WAIT_TIMEOUT", "120"))
CHAT_COMPLETION_POLL_INTERVAL = float(os.environ.get("CHAT_COMPLETION_POLL_INTERVAL", "0.5"))


def wait_for_request_result(request_id: str, timeout_seconds: float) -> Dict[str, Any]:
    """Wait for worker result and return the requests row.

    AxonHub/OpenAI clients expect /v1/chat/completions to return the final
    ChatCompletion object, not GPUHub's internal queued status. Keep the queue
    architecture but make the public OpenAI-compatible endpoint synchronous.
    """
    deadline = time.time() + timeout_seconds
    last_row = None

    while time.time() < deadline:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT status, output_ref, error_code, error_message
            FROM requests
            WHERE request_id = %s
            LIMIT 1
            """,
            (request_id,)
        )
        row = cursor.fetchone()
        conn.close()
        last_row = row

        if row and row.get("status") in {"succeeded", "failed", "cancelled", "timed_out"}:
            return row

        time.sleep(CHAT_COMPLETION_POLL_INTERVAL)

    return last_row or {"status": "timed_out", "error_message": "request not found"}


def openai_error(message: str, code: str = "gpu_hub_error", status_code: int = 500):
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "api_error",
                "code": code,
            }
        },
    )


# ==================== API 端点 ====================

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
    """OpenAI-compatible Chat API endpoint.

    Internally GPUHub still queues the task for a node agent, but the public
    provider-facing endpoint waits for the worker and returns the final
    ChatCompletion JSON so gateways such as AxonHub can parse choices[0].message.
    """
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
        (request_id, "senyao", "chat", "received", request.model_dump_json(), created_at)
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

    row = wait_for_request_result(request_id, CHAT_COMPLETION_WAIT_TIMEOUT)
    status = row.get("status")

    if status == "succeeded" and row.get("output_ref"):
        try:
            output = json.loads(row["output_ref"])
        except json.JSONDecodeError as exc:
            return openai_error(f"invalid worker JSON output: {exc}", "invalid_worker_output")

        # If the worker already returned an OpenAI ChatCompletion, pass it
        # through. This is the expected path for llama-guardian/llama.cpp.
        if isinstance(output, dict) and isinstance(output.get("choices"), list):
            output.setdefault("id", request_id)
            output.setdefault("object", "chat.completion")
            output.setdefault("created", int(created_at.timestamp()))
            output.setdefault("model", request.model)
            return output

        # Fallback for simple worker payloads: wrap text-like results.
        content = output.get("content") if isinstance(output, dict) else str(output)
        if content is None:
            content = json.dumps(output, ensure_ascii=False)
        
        # Estimate token counts for usage field (OpenAI compatibility)
        # Prompt tokens: approximate from messages
        prompt_tokens = 0
        for msg in request.messages:
            msg_content = msg.content if isinstance(msg.content, str) else str(msg.content)
            prompt_tokens += len(msg_content.split()) + 4  # rough estimate
        prompt_tokens += len(request.model.split()) + 1  # model name overhead
        
        # Completion tokens: approximate from generated content
        completion_tokens = len(content.split()) if content else 0
        
        return {
            "id": request_id,
            "object": "chat.completion",
            "created": int(created_at.timestamp()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    if status in {"failed", "cancelled", "timed_out"}:
        return openai_error(
            row.get("error_message") or f"GPUHub request {status}",
            row.get("error_code") or status,
            status_code=500,
        )

    return openai_error(
        f"GPUHub request timed out waiting for worker result: {request_id}",
        "timeout",
        status_code=504,
    )

EMBEDDING_WAIT_TIMEOUT = float(os.environ.get("EMBEDDING_WAIT_TIMEOUT", "60"))


@app.post("/v1/embeddings")
async def embeddings(request: EmbeddingRequest):
    """OpenAI-compatible Embedding API endpoint.

    Wait for worker result and return OpenAI-compatible embedding response.
    """
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
        (request_id, "senyao", "embedding", "received", request.model_dump_json(), created_at)
    )
    conn.commit()
    conn.close()
    
    # 加入 Redis 队列
    queue_item = {
        "request_id": request_id,
        "task_type": "embedding",
        "model": request.model,
        "priority": 1,
        "created_at": created_at.isoformat()
    }
    redis_client.lpush("gpuhub:queue", json.dumps(queue_item))

    # Wait for worker result
    row = wait_for_request_result(request_id, EMBEDDING_WAIT_TIMEOUT)
    status = row.get("status")

    if status == "succeeded" and row.get("output_ref"):
        try:
            output = json.loads(row["output_ref"])
        except json.JSONDecodeError as exc:
            return openai_error(f"invalid worker JSON output: {exc}", "invalid_worker_output")

        # If worker returned OpenAI embedding format, pass through
        if isinstance(output, dict) and isinstance(output.get("data"), list):
            output.setdefault("object", "list")
            output.setdefault("model", request.model)
            return output

        # Fallback: wrap embedding vector in OpenAI format
        # Estimate token count for usage
        input_text = request.input if isinstance(request.input, str) else " ".join(request.input)
        prompt_tokens = len(input_text.split()) + 1
        
        embedding_data = output.get("embedding") if isinstance(output, dict) else output
        if not isinstance(embedding_data, list):
            return openai_error("invalid embedding output format", "invalid_embedding")
        
        return {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": embedding_data,
                }
            ],
            "model": request.model,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "total_tokens": prompt_tokens,
            },
        }

    if status in {"failed", "cancelled", "timed_out"}:
        return openai_error(
            row.get("error_message") or f"GPUHub request {status}",
            row.get("error_code") or status,
            status_code=500,
        )

    return openai_error(
        f"GPUHub request timed out waiting for worker result: {request_id}",
        "timeout",
        status_code=504,
    )

STT_WAIT_TIMEOUT = float(os.environ.get("STT_WAIT_TIMEOUT", "120"))


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(...)
):
    """OpenAI-compatible STT API endpoint.

    Wait for worker result and return OpenAI-compatible transcription response.
    """
    request_id = str(uuid.uuid4())
    created_at = datetime.utcnow()
    
    # 读取音频文件（临时存储）
    audio_data = await file.read()
    audio_path = f"/tmp/{request_id}.{file.filename.split('.')[-1] if '.' in file.filename else 'wav'}"
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
        (request_id, "senyao", "stt", json.dumps({"audio_path": audio_path, "model": model}), created_at)
    )
    conn.commit()
    conn.close()
    
    # 加入 Redis 队列
    queue_item = {
        "request_id": request_id,
        "task_type": "stt",
        "model": model,
        "priority": 1,
        "created_at": created_at.isoformat()
    }
    redis_client.lpush("gpuhub:queue", json.dumps(queue_item))

    # Wait for worker result
    row = wait_for_request_result(request_id, STT_WAIT_TIMEOUT)
    status = row.get("status")

    if status == "succeeded" and row.get("output_ref"):
        try:
            output = json.loads(row["output_ref"])
        except json.JSONDecodeError as exc:
            return openai_error(f"invalid worker JSON output: {exc}", "invalid_worker_output")

        # If worker returned OpenAI transcription format, pass through
        if isinstance(output, dict) and "text" in output:
            output.setdefault("task", "transcribe")
            output.setdefault("language", "unknown")
            output.setdefault("model", model)
            return output

        # Fallback: wrap transcription text
        text = str(output) if not isinstance(output, dict) else output.get("text", str(output))
        return {
            "text": text,
            "task": "transcribe",
            "language": "unknown",
            "model": model,
        }

    if status in {"failed", "cancelled", "timed_out"}:
        return openai_error(
            row.get("error_message") or f"GPUHub request {status}",
            row.get("error_code") or status,
            status_code=500,
        )

    return openai_error(
        f"GPUHub request timed out waiting for worker result: {request_id}",
        "timeout",
        status_code=504,
    )

# ==================== 心跳与任务分发 ====================

@app.post("/heartbeat")
async def heartbeat(request: HeartbeatRequest):
    """接收 Node Agent 心跳"""
    node_id = request.node_id
    
    # 更新节点状态缓存
    nodes_status[node_id] = {
        "node_id": node_id,
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
    request_id = task_data.get("request_id")
    task_type = task_data.get("task_type")

    # Backward compatibility: older queue items only stored request_id. Infer
    # task_type from MySQL instead of crashing the node poller with KeyError.
    if request_id and not task_type:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT task_type FROM requests WHERE request_id = %s LIMIT 1",
            (request_id,)
        )
        row = cursor.fetchone()
        conn.close()
        task_type = row["task_type"] if row else None

    if not request_id or not task_type:
        print(f"[QUEUE WARNING] invalid queue item skipped: {task_data}")
        return {"task": None}
    
    # 选择 GPU（简化：选第一个可用）
    if not request.available_gpus:
        # Put the task back so it can be retried when a GPU is available.
        redis_client.rpush("gpuhub:queue", json.dumps(task_data))
        return {"task": None}

    selected_gpu_id = request.available_gpus[0]
    
    # 更新请求状态为 scheduled
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE requests SET status = %s, selected_node = %s, selected_gpu_ids = %s, updated_at = %s
        WHERE request_id = %s
        """,
        ("scheduled", request.node_id, str(selected_gpu_id), datetime.utcnow(), request_id)
    )
    conn.commit()
    conn.close()
    
    return {
        "task": {
            "request_id": request_id,
            "task_type": task_type,
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

# ==================== 前端静态文件 ====================

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# 挂载前端目录
app.mount("/static", StaticFiles(directory="frontend"), name="static")

# 根路径返回前端页面
@app.get("/", response_class=FileResponse)
def serve_frontend():
    return FileResponse("frontend/index.html")

# ==================== Scheduler 集成 ====================

import threading
from scheduler import Scheduler

# Scheduler实例（全局）
scheduler = None

def start_scheduler_thread():
    """启动Scheduler线程"""
    global scheduler
    print("🚀 启动 Scheduler 线程...")
    scheduler = Scheduler(redis_client, get_mysql_connection())
    scheduler.start()

# FastAPI启动事件
@app.on_event("startup")
def on_startup():
    """应用启动时启动Scheduler"""
    scheduler_thread = threading.Thread(target=start_scheduler_thread, daemon=True)
    scheduler_thread.start()
    print("✅ Scheduler 已启动")

# ==================== 启动 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)