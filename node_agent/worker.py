#!/usr/bin/env python3
"""GPUHub production node worker.

Canonical entrypoint for Worker node pull-mode worker.
Keeps http_server_v4 import for backward compatibility while removing versioned
entrypoint names from operator-facing scripts/services.
"""

from http_server_v4 import app, NODE_ID
import uvicorn

if __name__ == "__main__":
    print("🚀 GPUHub Node Agent 启动...")
    print("📍 监听端口: 8001")
    from http_server_v4 import CONTROL_PLANE_URL
    print(f"📍 Control Plane: {CONTROL_PLANE_URL}")
    print(f"📍 节点ID: {NODE_ID}")
    uvicorn.run(app, host="0.0.0.0", port=8001)
