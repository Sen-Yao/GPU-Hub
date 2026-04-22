# GPUHub API 协议

> **版本**: v1.0
> **用途**: 定义总控端（Control Plane）与算力端（Worker Node）的通信协议

---

## 一、架构概述

```
┌─────────────────────────────────────────────────────────────┐
│                      Client Layer                            │
│  (OpenClaw / 外部应用 / 用户) → HTTP Request                 │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│              Control Plane (总控端)                          │
│  FastAPI + Redis Queue + MySQL Tracking                     │
│  端点: /v1/chat/completions, /v1/embeddings, /v1/audio/...  │
└─────────────────────────────────────────────────────────────┘
                              ↓ (HTTP/WebSocket)
┌─────────────────────────────────────────────────────────────┐
│              Worker Node (算力端)                            │
│  Node Agent + Executors (llama, whisper)                    │
│  端点: 心跳上报 /heartbeat, 任务拉取 /fetch_task            │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、请求状态流转

### 2.1 状态定义

| 状态 | 英文 | 说明 |
|------|------|------|
| **已接收** | `received` | 请求已到达总控端 |
| **已验证** | `validated` | 参数验证通过 |
| **排队中** | `queued` | 加入 Redis 队列 |
| **已调度** | `scheduled` | 分配到某个节点 |
| **已分发** | `dispatched` | 任务发送到算力端 |
| **运行中** | `running` | 算力端正在执行 |
| **成功** | `succeeded` | 执行完成，返回结果 |
| **失败** | `failed` | 执行失败，返回错误 |
| **取消** | `cancelled` | 用户取消或超时 |
| **超时** | `timed_out` | 执行超时 |

### 2.2 状态流转图

```
received → validated → queued → scheduled → dispatched → running → succeeded/failed
                                      ↓
                              cancelled/timed_out
```

---

## 三、任务类型定义

### 3.1 Chat 任务

**API 端点**: `/v1/chat/completions`

**请求 Payload**：

```json
{
  "model": "llama-3.1-8b",
  "messages": [
    {"role": "user", "content": "你好"}
  ],
  "temperature": 0.7,
  "max_tokens": 2048
}
```

**响应 Payload**：

```json
{
  "request_id": "uuid-xxx",
  "status": "succeeded",
  "result": {
    "id": "chatcmpl-xxx",
    "object": "chat.completion",
    "choices": [
      {
        "index": 0,
        "message": {"role": "assistant", "content": "..."},
        "finish_reason": "stop"
      }
    ],
    "usage": {
      "prompt_tokens": 10,
      "completion_tokens": 50,
      "total_tokens": 60
    }
  }
}
```

### 3.2 Embedding 任务

**API 端点**: `/v1/embeddings`

**请求 Payload**：

```json
{
  "model": "text-embedding-3-small",
  "input": "这是一段文本"
}
```

### 3.3 STT 任务

**API 端点**: `/v1/audio/transcriptions`

**请求 Payload**（multipart/form-data）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `file` | binary | 音频文件（wav/mp3/m4a）|
| `model` | string | whisper-large-v3 |

---

## 四、心跳协议

### 4.1 Node Agent → Control Plane

**端点**: `/heartbeat`

**请求 Payload**：

```json
{
  "node_id": "hccs86-01",
  "timestamp": "2026-04-22T18:00:00Z",
  "gpu_status": [
    {
      "gpu_id": 0,
      "memory_used": 10240,
      "memory_total": 48128,
      "process_count": 2
    }
  ],
  "task_status": {
    "running_tasks": ["task-uuid-1"],
    "completed_tasks": 10,
    "failed_tasks": 1
  },
  "supported_tasks": ["chat", "embedding", "stt"]
}
```

### 4.2 心跳频率

- **默认**: 每 10 秒一次
- **任务执行中**: 每 5 秒一次
- **超时判定**: 60 秒无心跳 → 标记节点离线

---

## 五、队列协议

### 5.1 Redis 队列结构

**键名**: `gpuhub:queue`

**类型**: List（FIFO）

**元素格式**：

```json
{
  "request_id": "uuid-xxx",
  "task_type": "chat",
  "priority": 1,
  "created_at": "2026-04-22T18:00:00Z"
}
```

---

## 六、调度协议

### 6.1 调度策略（V1）

**策略**: FIFO + 显存判断

**步骤**：
1. 从队列取出任务（RPOP）
2. 查询所有节点心跳，获取 GPU 状态
3. 选择显存充足的 GPU（占用 < 46GB）
4. 分配任务到该节点
5. 无可用节点时，任务留在队列

---

## 七、错误码定义

| 错误码 | 说明 |
|--------|------|
| `VALIDATION_ERROR` | 参数验证失败 |
| `QUEUE_FULL` | 队列已满 |
| `NO_AVAILABLE_NODE` | 无可用节点 |
| `NODE_OFFLINE` | 节点离线 |
| `OOM` | 显存不足 |
| `TIMEOUT` | 执行超时 |
| `INTERNAL_ERROR` | 内部错误 |

---

_最后更新: 2026-04-22_