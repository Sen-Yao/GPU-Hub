#!/usr/bin/env python3
"""
Node Agent HTTP Server - production pull-mode worker

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
import base64
import tempfile
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from executor_manager import ExecutorManager

# ============== 配置 ==============

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://10.0.0.10:8003")
NODE_ID = os.environ.get("NODE_ID", "worker-node-01")
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL", "5"))  # 秒
FETCH_WAIT_SECONDS = int(os.environ.get("FETCH_WAIT_SECONDS", "25"))
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
MODELS_CONFIG_PATH = os.path.expanduser("~") + "/gpuhub/node_agent_v2/models.yaml"

# ============== FastAPI App ==============

app = FastAPI(title="GPUHub Node Agent", version="production")

executor_manager = ExecutorManager()

# 任务拉取线程控制
_stop_fetch_thread = False
_fetch_thread = None

# ============== 辅助函数 ==============

def cleanup_orphan_executors():
    """Clean orphan llama/whisper executors left by previous worker runs.

    Only touches GPUHub's local executor port pool (8100-8120) and only kills
    processes whose command line is llama-server or whisper-server. This avoids
    broad pkill patterns that could affect unrelated experiments.
    """
    try:
        output = subprocess.check_output(["ss", "-ltnp"], text=True, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"⚠️ 无法检查端口池残留 executor: {e}")
        return

    pids = set()
    for line in output.splitlines():
        if not any(f":{port}" in line for port in range(8100, 8121)):
            continue
        if "llama-server" not in line and "whisper-server" not in line:
            continue
        for marker in ("pid=",):
            start = 0
            while True:
                idx = line.find(marker, start)
                if idx < 0:
                    break
                idx += len(marker)
                end = idx
                while end < len(line) and line[end].isdigit():
                    end += 1
                if end > idx:
                    pids.add(int(line[idx:end]))
                start = end

    if not pids:
        print("✅ 未发现端口池残留 executor")
        return

    print(f"🧹 清理端口池残留 executor: {sorted(pids)}")
    for pid in sorted(pids):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"⚠️ SIGTERM {pid} 失败: {e}")
    time.sleep(2)
    for pid in sorted(pids):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        try:
            os.kill(pid, 9)
            print(f"⚠️ 强制清理残留 executor PID {pid}")
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"⚠️ SIGKILL {pid} 失败: {e}")


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


def _expand_model_path(path: str) -> str:
    """Expand ~ and environment variables in model paths."""
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path)))


def ensure_model_loaded(model: str, selected_gpu_id: int) -> bool:
    """Load model on demand before executing a pulled task.

    Node Agent v4 is the production pull-mode worker. It cannot rely on the
    legacy control-plane Scheduler to call /load_model, so it must close the
    lifecycle itself: fetch task -> load model if needed -> execute -> report.
    """
    if model in executor_manager.get_loaded_models():
        return True

    config = load_models_config()
    model_info = (config.get("models") or {}).get(model)
    if not model_info:
        print(f"❌ 模型配置不存在: {model}")
        return False

    model_path = _expand_model_path(model_info.get("path", ""))
    executor_type = model_info.get("executor", "llama.cpp")
    print(f"📦 按需加载模型: {model} -> GPU {selected_gpu_id}, path={model_path}")
    return executor_manager.load_model(
        model=model,
        model_path=model_path,
        gpu_ids=[selected_gpu_id],
        executor_type=executor_type,
    )


def worker_headers():
    if not WORKER_TOKEN:
        return {}
    return {"Authorization": f"Bearer {WORKER_TOKEN}"}

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
                "available_memory": available_memory,
                "gpu_status": gpu_status,
                "loaded_models": executor_manager.get_loaded_models(),
            }
            
            response = requests.post(
                f"{CONTROL_PLANE_URL}/fetch_task",
                params={"wait": FETCH_WAIT_SECONDS},
                json=payload,
                headers=worker_headers(),
                timeout=FETCH_WAIT_SECONDS + 15
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
        # Prefer task payload returned by /fetch_task. Avoid a second
        # dashboard/request round-trip, which can deadlock or stall when the
        # public synchronous API is waiting for this same worker result.
        if task.get("input_ref"):
            input_ref = json.loads(task["input_ref"]) if isinstance(task["input_ref"], str) else task["input_ref"]
        else:
            response = requests.get(
                f"{CONTROL_PLANE_URL}/dashboard/request/{request_id}",
                timeout=10
            )
            if response.status_code != 200:
                print(f"❌ 无法获取请求信息: {request_id}", flush=True)
                report_result(request_id, "failed", None, 0, "FETCH_ERROR", "Cannot fetch request details")
                return
            request_data = response.json()["request"]
            input_ref = json.loads(request_data["input_ref"])
        model = input_ref.get("model", "glm-4.5-air")
        
        print(f"🚀 执行任务: {request_id} (type={task_type}, model={model})")

        if not ensure_model_loaded(model, selected_gpu_id):
            run_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            report_result(request_id, "failed", None, run_ms, "MODEL_LOAD_FAILED", f"Cannot load model: {model}")
            print(f"❌ 模型加载失败: {request_id} ({model})")
            return
        
        # 执行
        if task_type == "chat":
            result = executor_manager.execute_chat(model, input_ref)
        elif task_type == "embedding":
            result = executor_manager.execute_embedding(model, input_ref)
        elif task_type == "stt":
            audio_path = input_ref.get("audio_path")
            if input_ref.get("audio_base64"):
                suffix = input_ref.get("audio_suffix") or "wav"
                audio_bytes = base64.b64decode(input_ref["audio_base64"])
                tmp = tempfile.NamedTemporaryFile(prefix=f"gpuhub-stt-{request_id}-", suffix=f".{suffix}", delete=False)
                try:
                    tmp.write(audio_bytes)
                    tmp.flush()
                    audio_path = tmp.name
                finally:
                    tmp.close()
            elif input_ref.get("audio_url"):
                suffix = input_ref.get("audio_suffix") or "wav"
                audio_url = input_ref["audio_url"]
                if audio_url.startswith("/"):
                    audio_url = f"{CONTROL_PLANE_URL}{audio_url}"
                response = requests.get(audio_url, headers=worker_headers(), timeout=120)
                if response.status_code != 200:
                    raise RuntimeError(f"Cannot download STT audio: HTTP {response.status_code} {response.text[:200]}")
                tmp = tempfile.NamedTemporaryFile(prefix=f"gpuhub-stt-{request_id}-", suffix=f".{suffix}", delete=False)
                try:
                    tmp.write(response.content)
                    tmp.flush()
                    audio_path = tmp.name
                finally:
                    tmp.close()
            if not audio_path:
                raise ValueError("STT task missing audio_path/audio_base64/audio_url")
            result = executor_manager.execute_stt(model, audio_path)
        else:
            raise ValueError(f"Unknown task type: {task_type}")
        
        run_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        
        if result and not result.get("error"):
            report_result(request_id, "succeeded", result, run_ms, selected_gpu_id=selected_gpu_id)
            print(f"✅ 任务完成: {request_id} (run_ms={run_ms})")
        else:
            error_msg = result.get("error", "Unknown error") if result else "Execution failed"
            report_result(request_id, "failed", None, run_ms, "EXECUTION_ERROR", str(error_msg), selected_gpu_id=selected_gpu_id)
            print(f"❌ 任务失败: {request_id} ({error_msg})")
        
    except Exception as e:
        run_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        report_result(request_id, "failed", None, run_ms, "EXECUTION_ERROR", str(e), selected_gpu_id=selected_gpu_id)
        print(f"❌ 任务执行异常: {request_id} ({e})")


def report_result(request_id: str, status: str, result: Optional[Dict], 
                  run_ms: int, error_code: Optional[str] = None,
                  error_message: Optional[str] = None,
                  selected_gpu_id: Optional[int] = None):
    """上报任务结果到 Control Plane.

    Keep worker reports minimal: raw facts only. Control Plane derives secondary metrics.
    """
    usage = result.get("usage", {}) if isinstance(result, dict) else {}
    payload = {
        "request_id": request_id,
        "node_id": NODE_ID,
        "status": status,
        "result": result,
        "run_ms": run_ms,
        "error_code": error_code,
        "error_message": error_message,
        "selected_gpu_id": selected_gpu_id,
        "actual_gpu_ids": [selected_gpu_id] if selected_gpu_id is not None else None,
        "input_tokens": usage.get("prompt_tokens") or usage.get("total_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "audio_duration_ms": int(float(result.get("duration", 0)) * 1000) if isinstance(result, dict) and result.get("duration") is not None else None,
    }
    
    try:
        response = requests.post(
            f"{CONTROL_PLANE_URL}/task_result",
            json=payload,
            headers=worker_headers(),
            timeout=30
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
    return {"status": "ok", "service": "GPUHub Node Agent", "mode": "pull"}

@app.on_event("startup")
def startup_event():
    """启动时清理残留 executor，并启动任务拉取线程"""
    global _fetch_thread, _stop_fetch_thread
    
    cleanup_orphan_executors()
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
    print("🚀 GPUHub Node Agent 启动...")
    print(f"📍 监听端口: 8001")
    print(f"📍 Control Plane: {CONTROL_PLANE_URL}")
    print(f"📍 节点ID: {NODE_ID}")
    
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")