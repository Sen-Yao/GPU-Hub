#!/usr/bin/env python3
"""
GPUHub MySQL Database Initialization

创建数据库和表结构
"""

import mysql.connector
import os

MYSQL_HOST = os.environ.get("MYSQL_HOST", "192.168.1.6")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", 3306))
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", None)

def init_database():
    conn = mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD
    )
    cursor = conn.cursor()
    
    # 创建数据库
    cursor.execute("CREATE DATABASE IF NOT EXISTS gpuhub")
    cursor.execute("USE gpuhub")
    
    # 创建 requests 表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            request_id VARCHAR(64) PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL,
            task_type VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            input_ref TEXT,
            output_ref TEXT,
            selected_node VARCHAR(64),
            selected_gpu_ids VARCHAR(128),
            queue_wait_ms INT,
            run_ms INT,
            error_code VARCHAR(32),
            error_message TEXT,
            retry_count INT DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME
        )
    """)
    
    # 创建 request_history 表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS request_history (
            history_id INT AUTO_INCREMENT PRIMARY KEY,
            request_id VARCHAR(64) NOT NULL,
            from_status VARCHAR(32),
            to_status VARCHAR(32) NOT NULL,
            timestamp DATETIME NOT NULL,
            message TEXT,
            FOREIGN KEY (request_id) REFERENCES requests(request_id)
        )
    """)
    
    conn.commit()
    conn.close()
    
    print("✅ 数据库初始化完成")

if __name__ == "__main__":
    init_database()