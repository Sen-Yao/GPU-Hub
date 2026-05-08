# Worker Deployment Checklist

> **Scope**: GPUHub Worker Node deployment, migration, and fallback-node preparation.
> **Production entrypoint**: `node_agent/http_server_v4.py` in pull mode.

GPUHub workers are GPU-dependent runtime nodes. Avoid treating worker deployment as a blind one-command install: each machine may have different GPUs, CUDA drivers, Python environments, model caches, network access, filesystem layout, and allowed model set.

Use this checklist before declaring a worker ready.

---

## Core principles

- **Confirm the node role first**: primary worker, fallback worker, embedding-only worker, STT-only worker, or mixed worker.
- **Only advertise models the node can actually serve**: generate `node_agent/models.yaml` from local capability; do not copy another node's config blindly.
- **Keep secrets out of Git and shell scripts**: do not hardcode `WORKER_TOKEN`; use a private environment file or runtime environment variable.
- **Debug in the foreground first**: only move to `nohup`, supervisor, or systemd after logs show the fetch loop and model loading behave correctly.
- **Prefer explicit paths**: record Python, `llama-server`, model, log, and config paths.

---

## 0. Deployment target

- [ ] Target host / SSH alias:
- [ ] Node ID:
- [ ] Control Plane URL:
- [ ] Node role:
  - [ ] Primary worker
  - [ ] Fallback worker
  - [ ] Embedding-only worker
  - [ ] STT-only worker
  - [ ] Mixed worker
- [ ] Models this node should support:
  - [ ] Chat model(s):
  - [ ] Embedding model(s):
  - [ ] STT model(s):
- [ ] Models this node must **not** advertise:
- [ ] Is file writing allowed on this host?
- [ ] Is dependency installation allowed?
- [ ] Is compiling `llama.cpp` allowed?
- [ ] Is model download allowed?
- [ ] Is starting a background service allowed?

---

## 1. Basic connectivity

- [ ] SSH works
- [ ] Hostname confirmed
- [ ] User identity confirmed
- [ ] Home directory confirmed
- [ ] Disk free space checked
- [ ] Control Plane reachable from the worker

Suggested commands:

```bash
hostname
whoami
df -h ~
curl -sS https://your-control-plane.example.com/health
```

Expected health shape:

```json
{"status":"healthy","redis":true,"mysql":true}
```

---

## 2. GPU and driver check

- [ ] `nvidia-smi` works
- [ ] GPU count confirmed
- [ ] GPU model(s) confirmed
- [ ] VRAM per GPU confirmed
- [ ] Current GPU memory usage checked
- [ ] Existing GPU processes checked
- [ ] Driver version recorded

Suggested commands:

```bash
nvidia-smi
nvidia-smi --query-gpu=index,name,memory.used,memory.total,driver_version --format=csv
nvidia-smi pmon -c 1
```

Record:

```text
GPU count:
GPU model(s):
VRAM:
Driver:
Current usage:
Deployment suitability:
```

---

## 3. Capability decision

Use VRAM and workload needs to decide what the node may advertise.

### Around 24 GB VRAM, single GPU

Usually suitable for:

- [x] Embedding models, depending on size
- [x] Faster-Whisper STT
- [ ] Large chat models
- [ ] Multiple resident models
- [ ] High concurrency

### 48 GB+ VRAM, single GPU

May be suitable for:

- [x] Embedding
- [x] STT
- [x] Medium or large chat models, depending on quantization and context size
- [ ] Multi-task concurrency, after load testing

### Multi-GPU worker

May support:

- [x] Per-model GPU assignment
- [x] Executor replicas
- [x] Mixed chat / embedding / STT workloads
- [x] Worker concurrency pools, after scheduler and local busy-state checks

---

## 4. Python / Conda / virtualenv check

- [ ] Python environment selected
- [ ] Python version recorded
- [ ] `fastapi` installed
- [ ] `uvicorn` installed
- [ ] `requests` installed
- [ ] `PyYAML` installed
- [ ] For STT: `faster-whisper` installed
- [ ] For STT: `ctranslate2` installed

Suggested commands:

```bash
command -v conda || true
find ~ -maxdepth 4 -type f -path "*/bin/python*" | head
/path/to/python --version
/path/to/pip list | egrep "fastapi|uvicorn|requests|PyYAML|faster|ctranslate|torch"
```

Record:

```text
Python path:
Pip path:
Environment manager:
Missing packages:
```

---

## 5. GPUHub code check

- [ ] `~/gpuhub` or chosen project directory exists
- [ ] Repository remote is correct
- [ ] Branch / commit recorded
- [ ] Local uncommitted changes checked
- [ ] Production worker files exist:
  - [ ] `node_agent/http_server_v4.py`
  - [ ] `node_agent/executor_manager.py`
  - [ ] `node_agent/models.yaml`

Suggested commands:

```bash
test -d ~/gpuhub && echo exists || echo missing
cd ~/gpuhub
git remote -v
git status --short
git log --oneline -5
```

Important:

- Do not blindly `git pull` over local modifications.
- Do not reuse old start scripts without checking the entrypoint.
- Current pull-mode worker should use `node_agent/http_server_v4.py`.
- Legacy push-mode / SSH scheduler paths are deprecated unless explicitly needed.

---

## 6. `llama.cpp` / `llama-server` check

Needed for GGUF-backed chat or embedding models.

- [ ] `llama-server` exists
- [ ] Path recorded
- [ ] CUDA build confirmed
- [ ] Can start with the target model
- [ ] Embedding endpoint works if serving embedding models

Suggested command:

```bash
find ~ -maxdepth 5 -type f -name llama-server 2>/dev/null
```

Record:

```text
LLAMA_SERVER_PATH:
CUDA support:
Needs build/copy/download:
```

If missing, choose one:

- [ ] Build on this host
- [ ] Copy from a compatible existing host
- [ ] Use a trusted prebuilt binary
- [ ] Disable GGUF-backed models for this node

---

## 7. Faster-Whisper check

Needed for `faster-whisper` STT models.

- [ ] `faster-whisper` Python package exists
- [ ] `ctranslate2` exists
- [ ] Model directory or Hugging Face cache exists
- [ ] Model can be loaded on CUDA
- [ ] A short audio transcription test passes

Suggested cache check:

```bash
find ~/.cache/huggingface/hub -maxdepth 2 -type d -name "models--*faster-whisper*"
```

Record:

```text
FASTER_WHISPER_MODEL_PATH:
Package versions:
Needs download:
```

---

## 8. Model file check

For every model to advertise:

- [ ] File or directory exists
- [ ] Path is readable by worker user
- [ ] Size looks plausible
- [ ] Required runtime exists
- [ ] VRAM estimate fits the node
- [ ] Test load succeeds

Example GGUF check:

```bash
ls -lh ~/models/embedding/your-embedding-model.gguf
```

If a model is missing, choose one:

- [ ] Copy from an existing worker
- [ ] Copy from shared storage
- [ ] Download from a public source
- [ ] Ask user to provide the model
- [ ] Do not advertise the model on this node

---

## 9. Generate `node_agent/models.yaml`

Generate this file from **local capability**, not from another worker.

Example embedding + STT worker:

```yaml
node_id: worker-node-01
local_port: 8001

models:
  embedding-model-id:
    path: ~/models/embedding/embedding-model.gguf
    vram_required: 5000
    executor: llama.cpp

  faster-whisper-large-v3:
    path: ~/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3
    vram_required: 6000
    executor: faster-whisper
```

Checklist:

- [ ] Node ID correct
- [ ] Only supported models listed
- [ ] Unsupported models omitted
- [ ] Paths exist
- [ ] Executor types correct
- [ ] VRAM estimates reasonable

---

## 10. Secret handling

- [ ] `WORKER_TOKEN` is not in Git
- [ ] `WORKER_TOKEN` is not hardcoded in `start.sh`
- [ ] Token is injected through a private environment file or runtime environment
- [ ] Private env file permission is `600`
- [ ] Logs do not print secrets

Recommended private env file:

```text
~/.config/gpuhub-worker/worker.env
```

Example:

```bash
CONTROL_PLANE_URL=https://your-control-plane.example.com
NODE_ID=worker-node-01
WORKER_TOKEN=replace-with-private-token
FETCH_WAIT_SECONDS=25
FETCH_INTERVAL=5
LLAMA_SERVER_PATH=/path/to/llama-server
```

Permissions:

```bash
chmod 600 ~/.config/gpuhub-worker/worker.env
```

---

## 11. Pre-start dry run

Before starting the worker:

- [ ] `node_agent/http_server_v4.py` exists
- [ ] `node_agent/models.yaml` exists
- [ ] Python env can import required packages
- [ ] `nvidia-smi` works
- [ ] Control Plane health check passes
- [ ] Token is available in environment
- [ ] Existing worker process checked
- [ ] Log path selected

Suggested package check:

```bash
/path/to/python - <<'PY'
import fastapi, uvicorn, requests, yaml
print("basic deps ok")
PY
```

---

## 12. Start worker

Start in the foreground first:

```bash
cd ~/gpuhub
set -a
source ~/.config/gpuhub-worker/worker.env
set +a
/path/to/python -u node_agent/http_server_v4.py
```

Watch for:

- [ ] FastAPI startup
- [ ] Fetch loop startup
- [ ] Control Plane request success
- [ ] GPU status reporting
- [ ] Token/auth errors
- [ ] `models.yaml` errors
- [ ] Model load errors

Only after foreground verification, run in the background:

```bash
nohup /path/to/python -u node_agent/http_server_v4.py > ~/gpuhub-agent.log 2>&1 &
```

If using systemd/supervisor, keep secrets in an env file and record the service definition.

---

## 13. Control Plane verification

- [ ] Target Node ID appears in `/dashboard/nodes`
- [ ] GPU status is correct
- [ ] Available models match `models.yaml`
- [ ] Unsupported models are not advertised
- [ ] Queue endpoint works
- [ ] No unexpected failed requests

Suggested commands:

```bash
curl -sS https://your-control-plane.example.com/dashboard/nodes
curl -sS https://your-control-plane.example.com/dashboard/queues
```

Expected:

```text
node_id: worker-node-01
available_models: only models supported by this worker
```

---

## 14. Smoke tests

### Embedding

- [ ] Submit embedding request
- [ ] Worker fetches task
- [ ] `llama-server` starts or is already running
- [ ] Embedding response returned
- [ ] No OOM
- [ ] Result reported to Control Plane

### STT

- [ ] Submit short audio transcription request
- [ ] Worker fetches task
- [ ] Faster-Whisper model loads
- [ ] Text response returned
- [ ] GPU memory usage reasonable
- [ ] Result reported to Control Plane

### Chat, if enabled

- [ ] Submit short chat request
- [ ] Model loads within expected VRAM
- [ ] Context size is appropriate
- [ ] Response returned
- [ ] No OOM

---

## 15. Deployment record

Record the final deployment state:

```text
Date:
Host:
Node ID:
GPU(s):
Driver:
Supported models:
Unsupported models:
Python path:
llama-server path:
Model paths:
Private env file path:
Start method:
Log path:
Control Plane verification:
Smoke test results:
Known limitations:
```

---

## Common pitfalls

- Copying another worker's `models.yaml` and advertising unsupported models.
- Hardcoding `WORKER_TOKEN` in scripts committed to Git.
- Starting old worker entrypoints instead of `node_agent/http_server_v4.py`.
- Assuming a 24 GB GPU can safely serve large chat models with long context.
- Running `git pull` over uncommitted local operational fixes.
- Moving directly to background execution before reading foreground logs.
- Treating model presence and model load success as the same thing.
