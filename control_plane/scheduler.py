#!/usr/bin/env python3
"""
GPUHub Scheduler - 任务调度器

职责:
- GPU动态分配(根据实时显存状态)
- 模型管理决策(加载/卸载时机)
- 任务排队与分发
- 缓存控制(空闲超时卸载)
- 通过SSH隧道推送指令

设计原则:
- 重总控端:所有决策在Scheduler完成
- Node Agent只执行,不决策
"""

import os
import json
import yaml
import time
import threading
import requests
import redis
import mysql.connector
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

# ============== 配置加载 ==============

def load_config():
    """加载总控端配置"""
    config_path = os.environ.get(
        "GPUHUB_SCHEDULER_CONFIG",
        "/app/config.yaml"  # 容器内路径
    )

    if not os.path.exists(config_path):
        print(f"⚠️ 配置文件不存在: {config_path}")
        return {}

    with open(config_path) as f:
        return yaml.safe_load(f)

# ============== 数据结构 ==============

@dataclass
class GPUStatus:
    """GPU状态"""
    gpu_id: int
    memory_used: int  # MB
    memory_total: int  # MB
    memory_free: int  # MB

@dataclass
class NodeStatus:
    """节点状态"""
    node_id: str
    instance_id: str
    last_heartbeat: datetime
    gpu_status: List[GPUStatus]
    loaded_models: List[str]
    supported_models: List[str]
    running_tasks: List[str]
    tunnel_port: int  # SSH隧道端口

@dataclass
class ModelConfig:
    """模型配置"""
    model: str
    vram_required: int  # MB
    supports: List[str]  # ["chat", "embedding", "stt"]
    executor: str  # "llama.cpp" or "whisper.cpp"
    model_path: str = ""  # 模型文件路径(在节点上)

@dataclass
class Task:
    """任务"""
    request_id: str
    task_type: str
    model: str
    input_ref: Dict
    priority: int
    created_at: datetime
    retry_count: int = 0

# ============== Scheduler 核心类 ==============

class Scheduler:
    """GPUHub任务调度器"""

    def __init__(self, redis_client, mysql_conn):
        self.config = load_config()
        self.redis = redis_client
        self.mysql = mysql_conn

        # 节点状态缓存
        self.nodes_status: Dict[str, NodeStatus] = {}

        # 模型配置
        self.models_config: Dict[str, ModelConfig] = {}
        self._load_models_config()

        # 节点配置
        self.nodes_config: Dict[str, Dict] = {}
        self._load_nodes_config()

        # 缓存控制
        self.cache_ttl = self.config.get("gpu_policy", {}).get("cache_ttl", 300)  # 5分钟
        self.model_load_time: Dict[str, datetime] = {}  # model -> last_load_time

        # 调度线程
        self.scheduler_thread = None
        self._stop_scheduler = False

        # 缓存清理线程
        self.cache_thread = None
        self._stop_cache = False

    def _load_models_config(self):
        """加载模型配置"""
        models = self.config.get("models", {})
        for model_name, model_data in models.items():
            self.models_config[model_name] = ModelConfig(
                model=model_name,
                vram_required=model_data.get("vram_required", 10000),
                supports=model_data.get("supports", []),
                executor=model_data.get("executor", "llama.cpp"),
                model_path=model_data.get("model_path", "")
            )

    def _load_nodes_config(self):
        """加载节点配置"""
        nodes = self.config.get("nodes", {})
        for node_id, node_data in nodes.items():
            self.nodes_config[node_id] = {
                "tunnel_port": node_data.get("local_tunnel_port", 9001),
                "gpu_count": node_data.get("gpu_count", 8)
            }

        # 加载SSH配置(用于远程执行)
        self.nodes_ssh_config = {}
        for node_id, node_data in nodes.items():
            self.nodes_ssh_config[node_id] = {
                "ssh_host": node_data.get("ssh_host"),
                "ssh_port": node_data.get("ssh_port", 22),
                "ssh_user": node_data.get("ssh_user", "root"),
                "local_tunnel_port": node_data.get("local_tunnel_port", 9001),
                "node_http_port": node_data.get("node_http_port", 8001)
            }

    def start(self):
        """启动调度器"""
        print("🚀 Scheduler 启动...")

        # 启动调度线程
        self._stop_scheduler = False
        self.scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            daemon=True
        )
        self.scheduler_thread.start()

        # 启动缓存清理线程
        self._stop_cache = False
        self.cache_thread = threading.Thread(
            target=self._cache_cleanup_loop,
            daemon=True
        )
        self.cache_thread.start()

        print("✅ Scheduler 已启动")

    def stop(self):
        """停止调度器"""
        print("🛑 停止 Scheduler...")

        self._stop_scheduler = True
        self._stop_cache = True

        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
        if self.cache_thread:
            self.cache_thread.join(timeout=5)

        print("✅ Scheduler 已停止")

    def _scheduler_loop(self):
        """调度循环"""
        while not self._stop_scheduler:
            try:
                # 1. 更新节点状态(主动查询)
                self._refresh_nodes_status()

                # 2. 从Redis队列取任务
                task = self._fetch_task()

                if task:
                    # 分发任务
                    self._dispatch_task(task)
            except Exception as e:
                print(f"❌ Scheduler loop error: {e}")
                import traceback
                traceback.print_exc()

            time.sleep(5)

    def _refresh_nodes_status(self):
        """主动查询节点状态"""
        for node_id, ssh_config in self.nodes_ssh_config.items():
            tunnel_port = ssh_config["local_tunnel_port"]

            # 使用宿主机IP(而不是容器内的localhost)
            tunnel_host = "192.168.1.6"  # Yggdrasil内网IP

            try:
                # 查询节点状态
                response = requests.get(
                    f"http://{tunnel_host}:{tunnel_port}/get_status",
                    timeout=5
                )

                if response.status_code == 200:
                    data = response.json()
                    node_gpu_status = data.get("gpu_status") or self._get_gpu_status_from_node(node_id)

                    # 构造心跳数据格式
                    heartbeat_data = {
                        "node_id": node_id,
                        "timestamp": datetime.utcnow().isoformat(),
                        "gpu_status": node_gpu_status,
                        "loaded_models": data.get("loaded_models", []),
                        "supported_models": list(self.models_config.keys()),
                        "running_tasks": [],
                        "instance_id": "scheduler-query"
                    }

                    # 更新节点状态
                    self.update_node_status(node_id, heartbeat_data)
                    # print(f"✅ 节点状态已更新: {node_id} ({len(data.get('loaded_models', []))} models loaded)")
                else:
                    print(f"⚠️ 节点查询失败: {node_id} (status={response.status_code})")
            except Exception as e:
                print(f"⚠️ 节点查询异常: {node_id} - {e}")

    def _get_gpu_status_from_node(self, node_id: str) -> List[Dict]:
        """从节点获取GPU状态(通过SSH执行nvidia-smi)"""
        ssh_config = self.nodes_ssh_config.get(node_id)

        if not ssh_config:
            return []

        try:
            import subprocess

            # SSH执行nvidia-smi
            ssh_cmd = f"ssh -p {ssh_config['ssh_port']} {ssh_config['ssh_user']}@{ssh_config['ssh_host']} nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader,nounits"

            result = subprocess.run(
                ssh_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                gpu_status = []
                for line in result.stdout.strip().split('\n'):
                    if line:
                        parts = line.split(',')
                        if len(parts) == 3:
                            memory_used = int(parts[1])
                            memory_total = int(parts[2])
                            gpu_status.append({
                                "gpu_id": int(parts[0]),
                                "memory_used": memory_used,
                                "memory_total": memory_total,
                                "memory_free": memory_total - memory_used  # 计算空闲显存
                            })
                return gpu_status
        except Exception as e:
            print(f"⚠️ GPU状态查询失败: {e}")

        return []

    def _cache_cleanup_loop(self):
        """缓存清理循环"""
        while not self._stop_cache:
            # 检查模型缓存超时
            self._check_cache_timeout()

            time.sleep(60)  # 每分钟检查

    def _fetch_task(self) -> Optional[Task]:
        """从Redis队列取任务"""
        try:
            # FIFO队列
            item = self.redis.rpop("gpuhub:queue")

            if item:
                data = json.loads(item)

                # 从MySQL获取完整任务信息
                request_id = data["request_id"]
                request_data = self._get_request_from_mysql(request_id)

                if request_data:
                    return Task(
                        request_id=request_id,
                        task_type=request_data["task_type"],
                        model=request_data.get("model", "glm-4.5-air"),
                        input_ref=json.loads(request_data["input_ref"]),
                        priority=data.get("priority", 1),
                        created_at=datetime.fromisoformat(data["created_at"]),
                        retry_count=data.get("retry_count", 0)
                    )
        except Exception as e:
            print(f"❌ 取任务失败: {e}")

        return None

    def _get_request_from_mysql(self, request_id: str) -> Optional[Dict]:
        """从MySQL获取请求信息"""
        try:
            cursor = self.mysql.cursor(dictionary=True)
            cursor.execute(
                "SELECT * FROM requests WHERE request_id = %s",
                (request_id,)
            )
            result = cursor.fetchone()
            cursor.close()
            return result
        except Exception as e:
            print(f"❌ MySQL查询失败: {e}")
            return None

    def _dispatch_task(self, task: Task):
        """分发任务"""
        print(f"📤 分发任务: {task.request_id} (type={task.task_type}, model={task.model})")

        # 1. 选择节点
        node = self._select_node(task)

        if not node:
            print(f"⚠️ 无可用节点,任务重新入队")
            self._requeue_task(task)
            return

        # 2. 选择GPU
        gpu_ids = self._select_gpu(node, task)

        if not gpu_ids:
            print(f"⚠️ 节点 {node.node_id} 无可用GPU")
            self._requeue_task(task)
            return

        # 3. 检查模型是否已加载
        if task.model not in node.loaded_models:
            # 4. 下发加载指令
            success = self._send_load_model(node, task.model, gpu_ids)

            if not success:
                print(f"❌ 模型加载失败")
                self._requeue_task(task)
                return

            # 等待加载完成(轮询节点状态)
            time.sleep(30)

            # 更新节点状态缓存
            node.loaded_models.append(task.model)
            self.model_load_time[f"{node.node_id}:{task.model}"] = datetime.utcnow()

        # 5. 下发执行指令。先标记 dispatched；如果 node_agent 同步返回
        # result，_send_execute_task 会进一步写成 succeeded/failed。
        self._update_task_status(task.request_id, "dispatched", node.node_id)
        success = self._send_execute_task(node, task, gpu_ids)
        
        if success:
            print(f"✅ 任务已分发到 {node.node_id}")
        else:
            print(f"❌ 任务执行失败")
            self._requeue_task(task)

    def _select_node(self, task: Task) -> Optional[NodeStatus]:
        """选择最优节点"""
        # 筛选在线节点
        online_nodes = []
        for node_id, status in self.nodes_status.items():
            # 检查心跳超时
            if datetime.utcnow() - status.last_heartbeat < timedelta(seconds=30):
                online_nodes.append(status)

        if not online_nodes:
            return None

        # 按优先级排序:
        # 1. 已加载目标模型的节点优先
        # 2. 空闲显存最大的节点优先

        # 检查是否有节点已加载模型
        preloaded_nodes = [n for n in online_nodes if task.model in n.loaded_models]

        if preloaded_nodes:
            # 选择空闲显存最大的
            return max(preloaded_nodes, key=lambda n: self._get_total_free_memory(n))

        # 否则选择空闲显存最大的
        return max(online_nodes, key=lambda n: self._get_total_free_memory(n))

    def _select_gpu(self, node: NodeStatus, task: Task) -> List[int]:
        """选择最优GPU"""
        model_config = self.models_config.get(task.model)

        if not model_config:
            print(f"⚠️ 模型配置不存在: {task.model}")
            return []

        vram_required = model_config.vram_required

        # 筛选可用GPU(空闲显存 > 需求)
        available_gpus = []
        for gpu in node.gpu_status:
            if gpu.memory_free >= vram_required:
                available_gpus.append(gpu)

        if not available_gpus:
            gpu_summary = [
                f"gpu{gpu.gpu_id}:free={gpu.memory_free}MB,total={gpu.memory_total}MB"
                for gpu in node.gpu_status
            ]
            print(
                f"⚠️ 无满足显存需求的GPU: node={node.node_id}, "
                f"model={task.model}, required={vram_required}MB, gpus={gpu_summary}"
            )
            return []

        # 选择空闲显存最大的GPU(单一GPU)
        best_gpu = max(available_gpus, key=lambda g: g.memory_free)

        return [best_gpu.gpu_id]

    def _get_total_free_memory(self, node: NodeStatus) -> int:
        """计算节点总空闲显存"""
        return sum(gpu.memory_free for gpu in node.gpu_status)

    def _send_load_model(self, node: NodeStatus, model: str, gpu_ids: List[int]) -> bool:
        """下发加载模型指令(通过SSH隧道)"""
        # 获取节点SSH配置
        node_ssh_config = self.nodes_ssh_config.get(node.node_id)

        if not node_ssh_config:
            print(f"⚠️ 节点SSH配置不存在: {node.node_id}")
            return False

        # 方案1:通过SSH隧道推送
        tunnel_port = node_ssh_config["local_tunnel_port"]

        # 方案2:直接SSH执行(更简单)
        # ssh root@node "curl -X POST localhost:8001/load_model -d ..."

        model_config = self.models_config.get(model)
        model_path = model_config.model_path if model_config else ""

        payload = {
            "model": model,
            "model_path": model_path,
            "gpu_ids": gpu_ids,
            "executor_type": model_config.executor if model_config else "llama.cpp"
        }

        # 转义JSON字符串(用于shell命令)
        payload_json = json.dumps(payload).replace('"', '\\"')

        # SSH命令:远程执行curl
        ssh_cmd = f"ssh -p {node_ssh_config['ssh_port']} {node_ssh_config['ssh_user']}@{node_ssh_config['ssh_host']} curl -X POST http://localhost:8001/load_model -H 'Content-Type: application/json' -d '{payload_json}'"

        try:
            # 方案1:通过本地隧道端口(如果隧道已建立)
            response = requests.post(
                f"http://localhost:{tunnel_port}/load_model",
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                print(f"✅ load_model指令已推送({model} → GPU {gpu_ids})")
                return True
            else:
                print(f"❌ load_model失败: {response.status_code}")
                return False
        except Exception as e:
            # 阶段2:直接SSH执行
            print(f"⚠️ 隧道推送失败,尝试直接SSH执行: {e}")

            import subprocess
            result = subprocess.run(
                ssh_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                print(f"✅ load_model通过SSH执行成功")
                return True
            else:
                print(f"❌ SSH执行失败: {result.stderr}")
                return False

    def _send_execute_task(self, node: NodeStatus, task: Task, gpu_ids: List[int]) -> bool:
        """下发执行任务指令"""
        tunnel_port = self.nodes_config.get(node.node_id, {}).get("tunnel_port", 9001)

        payload = {
            "task_id": task.request_id,
            "model": task.model,
            "task_type": task.task_type,
            "input": task.input_ref
        }

        try:
            response = requests.post(
                f"http://192.168.1.6:{tunnel_port}/execute_task",
                json=payload,
                timeout=120
            )

            if response.status_code == 200:
                print(f"✅ execute_task指令已推送")
                try:
                    data = response.json()
                except Exception:
                    data = {}
                # Current node_agent_v2 executes synchronously and returns the
                # result directly instead of POSTing /task_result. Persist that
                # result here so provider-facing endpoints can unblock.
                if isinstance(data, dict) and "result" in data:
                    self._store_task_result(task.request_id, "succeeded", data.get("result"))
                return True
            else:
                print(f"❌ execute_task失败: {response.status_code} {response.text[:500]}")
                return False
        except Exception as e:
            print(f"❌ execute_task推送异常: {e}")
            return False

    def _store_task_result(self, request_id: str, status: str, result: Optional[Dict],
                           error_code: Optional[str] = None,
                           error_message: Optional[str] = None):
        """Persist synchronously returned node execution result."""
        try:
            cursor = self.mysql.cursor()
            output_ref = json.dumps(result) if result is not None else None
            cursor.execute(
                """
                UPDATE requests
                SET status = %s, output_ref = %s, error_code = %s,
                    error_message = %s, updated_at = %s
                WHERE request_id = %s
                """,
                (status, output_ref, error_code, error_message, datetime.utcnow(), request_id)
            )
            self.mysql.commit()
            cursor.close()
        except Exception as e:
            print(f"❌ 写入任务结果失败: {e}")

    def _requeue_task(self, task: Task):
        """任务重新入队"""
        task.retry_count += 1

        if task.retry_count > 3:
            print(f"❌ 任务重试次数超过上限: {task.request_id}")
            self._update_task_status(task.request_id, "failed", None, "MAX_RETRY_EXCEEDED")
            return

        # 重新入队
        queue_item = {
            "request_id": task.request_id,
            "priority": task.priority,
            "created_at": task.created_at.isoformat(),
            "retry_count": task.retry_count
        }

        self.redis.lpush("gpuhub:queue", json.dumps(queue_item))
        print(f"🔄 任务重新入队(retry_count={task.retry_count})")

    def _update_task_status(self, request_id: str, status: str, node_id: Optional[str],
                             error_code: Optional[str] = None):
        """更新任务状态"""
        try:
            cursor = self.mysql.cursor()
            cursor.execute(
                """
                UPDATE requests
                SET status = %s, selected_node = %s, error_code = %s, updated_at = %s
                WHERE request_id = %s
                """,
                (status, node_id, error_code, datetime.utcnow(), request_id)
            )
            self.mysql.commit()
            cursor.close()
        except Exception as e:
            print(f"❌ 更新任务状态失败: {e}")

    def _check_cache_timeout(self):
        """检查模型缓存超时"""
        now = datetime.utcnow()

        for key, load_time in self.model_load_time.items():
            node_id, model = key.split(":")

            # 检查是否超时
            if now - load_time > timedelta(seconds=self.cache_ttl):
                # 检查节点是否仍在运行任务
                node = self.nodes_status.get(node_id)

                if node and model in node.loaded_models:
                    # 检查是否有任务在使用该模型
                    if model not in self._get_active_models():
                        # 下发卸载指令
                        print(f"🗑️ 模型缓存超时,下发卸载指令: {model}")
                        self._send_unload_model(node, model)

                        # 移除缓存记录
                        del self.model_load_time[key]

    def _get_active_models(self) -> List[str]:
        """获取当前活跃模型(有任务在使用)"""
        # 从队列检查
        active_models = []

        # 查看队列中的任务
        queue_length = self.redis.llen("gpuhub:queue")
        if queue_length > 0:
            # 预览队列(不取出)
            items = self.redis.lrange("gpuhub:queue", 0, queue_length - 1)
            for item in items:
                data = json.loads(item)
                request_id = data["request_id"]
                # 从MySQL获取model(简化:假设队列项包含model)
                # 实际需要查询MySQL

        return active_models

    def _send_unload_model(self, node: NodeStatus, model: str) -> bool:
        """下发卸载模型指令"""
        tunnel_port = self.nodes_config.get(node.node_id, {}).get("tunnel_port", 9001)

        payload = {"model": model}

        try:
            response = requests.post(
                f"http://192.168.1.6:{tunnel_port}/unload_model",
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                print(f"✅ unload_model指令已推送({model})")

                # 更新节点状态
                if model in node.loaded_models:
                    node.loaded_models.remove(model)

                return True
            else:
                print(f"❌ unload_model失败: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ unload_model推送异常: {e}")
            return False

    def update_node_status(self, node_id: str, heartbeat_data: Dict):
        """更新节点状态(从心跳)"""
        # 解析心跳数据。部分 node agent 只上报运行/模型状态,不上报 GPU
        # 显存;这种情况下不要用空列表覆盖 scheduler 主动 SSH 查询到的
        # GPU 状态,否则会误判"无可用 GPU"。
        gpu_status = []
        heartbeat_gpu_data = heartbeat_data.get("gpu_status") or []
        for gpu_data in heartbeat_gpu_data:
            gpu_status.append(GPUStatus(
                gpu_id=gpu_data["gpu_id"],
                memory_used=gpu_data["memory_used"],
                memory_total=gpu_data["memory_total"],
                memory_free=gpu_data.get("memory_free",
                                         gpu_data["memory_total"] - gpu_data["memory_used"])
            ))

        if not gpu_status and node_id in self.nodes_status:
            gpu_status = self.nodes_status[node_id].gpu_status

        # 检查instance_id变化(重启检测)
        old_instance_id = self.nodes_status.get(node_id).instance_id if node_id in self.nodes_status else None
        new_instance_id = heartbeat_data.get("instance_id", "")

        if old_instance_id and old_instance_id != new_instance_id:
            print(f"⚠️ 节点重启检测: {node_id} (old={old_instance_id}, new={new_instance_id})")
            # 清空旧状态
            self._clear_node_state(node_id)

        # 更新状态
        self.nodes_status[node_id] = NodeStatus(
            node_id=node_id,
            instance_id=new_instance_id,
            last_heartbeat=datetime.fromisoformat(heartbeat_data["timestamp"]),
            gpu_status=gpu_status,
            loaded_models=heartbeat_data.get("loaded_models", []),
            supported_models=heartbeat_data.get("supported_models", []),
            running_tasks=heartbeat_data.get("running_tasks", []),
            tunnel_port=self.nodes_config.get(node_id, {}).get("tunnel_port", 9001)
        )

    def _clear_node_state(self, node_id: str):
        """清空节点状态(重启后)"""
        if node_id in self.nodes_status:
            del self.nodes_status[node_id]

        # 清空模型加载记录
        keys_to_remove = [k for k in self.model_load_time if k.startswith(f"{node_id}:")]
        for key in keys_to_remove:
            del self.model_load_time[key]

        print(f"🧹 节点状态已清空: {node_id}")

# ============== 测试入口 ==============

if __name__ == "__main__":
    print("🧪 测试 Scheduler...")

    # 需要Redis和MySQL连接
    # scheduler = Scheduler(redis_client, mysql_conn)
    # scheduler.start()

    print("✅ Scheduler 代码已创建(待集成)")