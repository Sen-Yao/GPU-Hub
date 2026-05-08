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
import base64
import time
import re
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Header, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
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


STT_UPLOAD_DIR = Path(os.environ.get("STT_UPLOAD_DIR", "/tmp/gpuhub-stt-uploads"))
STT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STT_UPLOAD_TTL_SECONDS = int(os.environ.get("STT_UPLOAD_TTL_SECONDS", "86400"))

def safe_audio_suffix(filename: Optional[str]) -> str:
    if filename and "." in filename:
        suffix = filename.rsplit(".", 1)[-1].lower()
        suffix = re.sub(r"[^a-z0-9]", "", suffix)
        return suffix[:12] or "wav"
    return "wav"

def stt_audio_path(request_id: str, suffix: str) -> Path:
    return STT_UPLOAD_DIR / f"{request_id}.{safe_audio_suffix(suffix)}"

def cleanup_stale_stt_uploads(max_age_seconds: int = STT_UPLOAD_TTL_SECONDS):
    """Best-effort cleanup for uploaded STT temp files."""
    now = time.time()
    removed = 0
    try:
        for path in STT_UPLOAD_DIR.iterdir():
            if not path.is_file():
                continue
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink()
                removed += 1
    except Exception as exc:
        print(f"[STT CLEANUP WARNING] {exc}")
    if removed:
        print(f"[STT CLEANUP] removed {removed} stale upload files")

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
    # Optional raw node-side observations for future global scheduling.
    gpu_status: Optional[List[Dict[str, Any]]] = None
    loaded_models: Optional[Any] = None

class TaskResultRequest(BaseModel):
    request_id: str
    node_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    run_ms: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    # Keep worker reports simple: raw facts only. Control Plane derives secondary metrics.
    selected_gpu_id: Optional[int] = None
    actual_gpu_ids: Optional[List[int]] = None
    load_ms: Optional[int] = None
    execute_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    audio_duration_ms: Optional[int] = None

# 节点状态缓存（内存 + Redis）
nodes_status = {}

def update_node_observation(
    node_id: str,
    *,
    timestamp: Optional[str] = None,
    gpu_status: Optional[List[Dict[str, Any]]] = None,
    task_status: Optional[Dict[str, Any]] = None,
    supported_tasks: Optional[List[str]] = None,
    loaded_models: Optional[Any] = None,
    source: str = "unknown",
):
    """Refresh node presence for dashboard observability.

    Pull-mode workers may not call /heartbeat continuously, but every
    /fetch_task request carries fresh GPU/model observations. Treat that as
    a valid node presence signal so /dashboard/nodes reflects workers that
    are actively polling and executing tasks.
    """
    if not node_id:
        return

    existing = nodes_status.get(node_id, {})
    observed_at = timestamp or datetime.utcnow().isoformat()
    node_status = {
        "node_id": node_id,
        "last_heartbeat": observed_at,
        "last_seen": observed_at,
        "source": source,
        "gpu_status": gpu_status if gpu_status is not None else existing.get("gpu_status", []),
        "task_status": task_status if task_status is not None else existing.get("task_status", {}),
        "supported_tasks": supported_tasks if supported_tasks is not None else existing.get("supported_tasks", []),
        "loaded_models": loaded_models if loaded_models is not None else existing.get("loaded_models", []),
    }
    nodes_status[node_id] = node_status
    redis_client.set(f"gpuhub:node:{node_id}", json.dumps(node_status), ex=120)

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
    try:
        body = await request.body()
        body_text = body.decode("utf-8", errors="replace")[:4000]
    except Exception:
        body_text = "<client disconnected before validation body could be read>"
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
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
if not WORKER_TOKEN:
    print("⚠️ WORKER_TOKEN 环境变量未设置，worker endpoints are open")

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


SCHEDULER_POLICY = os.environ.get("GPUHUB_SCHEDULER_POLICY", "auto").lower()
SCHEDULER_ALLOWED_POLICIES = {"auto", "max_free_vram", "binpack", "balanced", "manual"}
DEFAULT_SAFETY_MARGIN_MB = int(os.environ.get("GPUHUB_SCHEDULER_SAFETY_MARGIN_MB", "4096"))
STT_SAFETY_MARGIN_MB = int(os.environ.get("GPUHUB_STT_SAFETY_MARGIN_MB", "8192"))
MODEL_VRAM_REQUIREMENTS = {
    # Conservative defaults; per-model values can be moved to DB/config later.
    "systran-faster-whisper-large-v3": int(os.environ.get("GPUHUB_STT_LARGE_V3_REQUIRED_MB", "16000")),
    "faster-whisper-large-v3": int(os.environ.get("GPUHUB_STT_LARGE_V3_REQUIRED_MB", "16000")),
    "whisper-large-v3": int(os.environ.get("GPUHUB_WHISPER_LARGE_V3_REQUIRED_MB", "12000")),
    "Qwen3-Embedding-8B": int(os.environ.get("GPUHUB_QWEN3_EMBED_REQUIRED_MB", "12000")),
}


def get_scheduler_settings() -> Dict[str, Any]:
    policy = SCHEDULER_POLICY if SCHEDULER_POLICY in SCHEDULER_ALLOWED_POLICIES else "auto"
    return {
        "policy": policy,
        "default_policy": "auto",
        "allowed_policies": ["auto", "max_free_vram", "binpack", "balanced", "manual"],
        "safety_margin_mb": DEFAULT_SAFETY_MARGIN_MB,
        "stt_safety_margin_mb": STT_SAFETY_MARGIN_MB,
    }


def required_vram_mb(task_type: str, model: str) -> int:
    base = MODEL_VRAM_REQUIREMENTS.get(model, 0)
    margin = STT_SAFETY_MARGIN_MB if task_type == "stt" else DEFAULT_SAFETY_MARGIN_MB
    return base + margin


def auto_policy_for_task(task_type: str) -> str:
    if task_type == "stt":
        return "max_free_vram"
    if task_type in {"chat", "embedding"}:
        return "model_affinity"
    return "max_free_vram"


def select_gpu_for_task(task_type: str, model: str, request: FetchTaskRequest) -> Dict[str, Any]:
    candidates = []
    for idx, gpu_id in enumerate(request.available_gpus or []):
        free_mb = None
        if idx < len(request.available_memory or []):
            free_mb = request.available_memory[idx]
        elif request.gpu_status:
            for gpu in request.gpu_status:
                if gpu.get("gpu_id") == gpu_id:
                    free_mb = gpu.get("memory_free")
                    break
        if free_mb is None:
            free_mb = 0
        candidates.append({"node_id": request.node_id, "gpu_id": gpu_id, "free_mb": int(free_mb)})

    need_mb = required_vram_mb(task_type, model)
    eligible = [c for c in candidates if c["free_mb"] >= need_mb] if need_mb > 0 else candidates[:]
    policy = get_scheduler_settings()["policy"]
    effective_policy = auto_policy_for_task(task_type) if policy == "auto" else policy

    # Minimal implementation now: model_affinity falls back to max-free until
    # loaded_models has per-GPU detail. The policy name is preserved for future global scheduling.
    pool = eligible or candidates
    if not pool:
        return {
            "selected_gpu_id": None,
            "scheduler_info": {
                "policy": policy,
                "effective_policy": effective_policy,
                "required_mb": need_mb,
                "candidates": candidates,
                "reason": "no candidate GPUs reported by worker",
            },
        }

    if effective_policy == "binpack":
        # Choose the smallest free GPU that still fits, reducing fragmentation.
        selected = min(pool, key=lambda c: c["free_mb"])
    else:
        # auto STT / max_free_vram / balanced initial fallback: choose safest GPU.
        selected = max(pool, key=lambda c: c["free_mb"])

    reason = f"selected GPU{selected['gpu_id']} by {effective_policy}; free={selected['free_mb']}MB required={need_mb}MB"
    if not eligible and need_mb > 0:
        reason += "; no GPU satisfied requirement, using best-effort max available"

    return {
        "selected_gpu_id": selected["gpu_id"],
        "scheduler_info": {
            "policy": policy,
            "effective_policy": effective_policy,
            "required_mb": need_mb,
            "candidates": candidates,
            "eligible": eligible,
            "selected": selected,
            "reason": reason,
        },
    }


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
        (request_id, "default-user", "chat", "received", request.model_dump_json(), created_at)
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

    row = await run_in_threadpool(wait_for_request_result, request_id, CHAT_COMPLETION_WAIT_TIMEOUT)
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
        (request_id, "default-user", "embedding", "received", request.model_dump_json(), created_at)
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
    row = await run_in_threadpool(wait_for_request_result, request_id, EMBEDDING_WAIT_TIMEOUT)
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
    
    cleanup_stale_stt_uploads()

    # 读取音频文件。长音频不要塞进 MySQL input_ref（会触发 Data too long），
    # 改为落盘到 Control Plane 本地临时目录；worker 通过受 worker token 保护的
    # internal endpoint 下载。input_ref 只保存短 JSON 引用。
    audio_data = await file.read()
    suffix = safe_audio_suffix(file.filename)
    audio_path = stt_audio_path(request_id, suffix)
    audio_path.write_bytes(audio_data)
    input_ref = {
        "model": model,
        "audio_filename": file.filename or f"{request_id}.{suffix}",
        "audio_suffix": suffix,
        "audio_size": len(audio_data),
        "audio_url": f"/internal/stt_audio/{request_id}/{suffix}",
    }
    
    # 存入 MySQL
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO requests (request_id, user_id, task_type, status, input_ref, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (request_id, "default-user", "stt", "queued", json.dumps(input_ref), created_at)
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
    row = await run_in_threadpool(wait_for_request_result, request_id, STT_WAIT_TIMEOUT)
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


def verify_worker_token(authorization: Optional[str] = Header(default=None), x_gpuhub_worker_token: Optional[str] = Header(default=None)):
    """Verify worker token for internal worker protocol endpoints."""
    if not WORKER_TOKEN:
        return True
    token = x_gpuhub_worker_token or ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(token, WORKER_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid worker token")
    return True


@app.get("/internal/stt_audio/{request_id}/{suffix}")
async def internal_stt_audio(request_id: str, suffix: str, _worker_auth: bool = Depends(verify_worker_token)):
    """Serve uploaded STT audio to pull-mode workers. Protected by worker token."""
    safe_request_id = re.sub(r"[^a-fA-F0-9-]", "", request_id)
    path = stt_audio_path(safe_request_id, suffix)
    if not path.exists():
        raise HTTPException(status_code=404, detail="audio file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)

# ==================== 心跳与任务分发 ====================

@app.post("/heartbeat")
async def heartbeat(request: HeartbeatRequest, _worker_auth: bool = Depends(verify_worker_token)):
    """接收 Node Agent 心跳"""
    update_node_observation(
        request.node_id,
        timestamp=request.timestamp,
        gpu_status=request.gpu_status,
        task_status=request.task_status,
        supported_tasks=request.supported_tasks,
        source="heartbeat",
    )

    return {
        "acknowledged": True,
        "assigned_tasks": [],
        "commands": []
    }

@app.post("/fetch_task")
async def fetch_task(request: FetchTaskRequest, wait: int = 0, _worker_auth: bool = Depends(verify_worker_token)):
    """Node Agent 拉取任务"""
    update_node_observation(
        request.node_id,
        gpu_status=request.gpu_status,
        loaded_models=request.loaded_models,
        source="fetch_task",
    )

    # 从队列取出任务；wait>0 时使用 Redis BRPOP 实现长轮询，减少空轮询噪音。
    wait = max(0, min(int(wait or 0), 30))
    if wait > 0:
        # Redis BRPOP is blocking. Run it off the asyncio event loop so long-polling
        # workers do not starve /health, /dashboard/*, or other API requests.
        popped = await run_in_threadpool(redis_client.brpop, "gpuhub:queue", timeout=wait)
        queue_item = popped[1] if popped else None
    else:
        queue_item = await run_in_threadpool(redis_client.rpop, "gpuhub:queue")

    if not queue_item:
        return {"task": None}

    task_data = json.loads(queue_item)
    request_id = task_data.get("request_id")
    task_type = task_data.get("task_type")

    # Backward compatibility: older queue items only stored request_id. Infer
    # task_type from MySQL instead of crashing the node poller with KeyError.
    request_row = None
    if request_id:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT task_type, input_ref FROM requests WHERE request_id = %s LIMIT 1",
            (request_id,)
        )
        request_row = cursor.fetchone()
        conn.close()
        if not task_type and request_row:
            task_type = request_row["task_type"]

    if not request_id or not task_type or not request_row:
        print(f"[QUEUE WARNING] invalid queue item skipped: {task_data}")
        return {"task": None}
    
    model = task_data.get("model")
    if not model and request_row:
        try:
            input_data = json.loads(request_row.get("input_ref") or "{}")
            model = input_data.get("model")
        except Exception:
            model = None
    model = model or ""

    # Select GPU using a policy-aware single-node algorithm that is ready for future multi-node candidates.
    selection = select_gpu_for_task(task_type, model, request)
    selected_gpu_id = selection.get("selected_gpu_id")
    if selected_gpu_id is None:
        redis_client.rpush("gpuhub:queue", json.dumps(task_data))
        return {"task": None}
    scheduler_info = selection.get("scheduler_info", {})
    
    # 更新请求状态为 scheduled
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE requests SET status = %s, selected_node = %s, selected_gpu_ids = %s, scheduler_info = %s, updated_at = %s
        WHERE request_id = %s
        """,
        ("scheduled", request.node_id, json.dumps([selected_gpu_id]), json.dumps(scheduler_info), datetime.utcnow(), request_id)
    )
    conn.commit()
    conn.close()
    print(f"[SCHEDULER] request={request_id} task={task_type} model={model} {scheduler_info.get('reason')}")
    
    return {
        "task": {
            "request_id": request_id,
            "task_type": task_type,
            "selected_gpu_id": selected_gpu_id,
            "input_ref": request_row.get("input_ref") if request_row else None
        }
    }

@app.post("/task_result")
async def task_result(request: TaskResultRequest, _worker_auth: bool = Depends(verify_worker_token)):
    """接收 Node Agent 任务结果"""
    # 更新请求状态
    conn = get_mysql_connection()
    cursor = conn.cursor()
    
    status = request.status
    output_ref = json.dumps(request.result) if request.result else None
    error_code = request.error_code
    error_message = request.error_message
    run_ms = request.run_ms
    runtime_metrics = {
        k: v for k, v in {
            "run_ms": request.run_ms,
            "load_ms": request.load_ms,
            "execute_ms": request.execute_ms,
            "input_tokens": request.input_tokens,
            "output_tokens": request.output_tokens,
            "audio_duration_ms": request.audio_duration_ms,
        }.items() if v is not None
    }
    resource_usage = {
        k: v for k, v in {
            "node_id": request.node_id,
            "selected_gpu_id": request.selected_gpu_id,
            "actual_gpu_ids": request.actual_gpu_ids,
        }.items() if v is not None
    }
    
    cursor.execute(
        """
        UPDATE requests 
        SET status = %s, output_ref = %s, error_code = %s, error_message = %s, run_ms = %s,
            runtime_metrics = %s, resource_usage = %s, updated_at = %s
        WHERE request_id = %s
        """,
        (status, output_ref, error_code, error_message, run_ms,
         json.dumps(runtime_metrics) if runtime_metrics else None,
         json.dumps(resource_usage) if resource_usage else None,
         datetime.utcnow(), request.request_id)
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

@app.get("/dashboard/settings")
async def dashboard_settings():
    """Scheduler settings exposed for frontend display/selection placeholder."""
    return {"scheduler": get_scheduler_settings()}

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
ENABLE_INTERNAL_SCHEDULER = os.environ.get("ENABLE_INTERNAL_SCHEDULER", "false").lower() == "true"

@app.on_event("startup")
def on_startup():
    """应用启动事件。

    Production uses node-agent pull mode (/fetch_task -> /task_result).
    The legacy internal Scheduler pushes tasks through the SSH tunnel path
    and must stay disabled unless explicitly requested, otherwise it can race
    node agents for Redis queue items and break request lifecycle closure.
    """
    if ENABLE_INTERNAL_SCHEDULER:
        scheduler_thread = threading.Thread(target=start_scheduler_thread, daemon=True)
        scheduler_thread.start()
        print("✅ Internal Scheduler 已启动")
    else:
        print("⏸️ Internal Scheduler disabled; using node-agent pull mode")

# ==================== 启动 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
