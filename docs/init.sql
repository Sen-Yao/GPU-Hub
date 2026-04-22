-- GPUHub 数据库初始化 SQL
-- 请在 MariaDB/MySQL 中执行

CREATE DATABASE IF NOT EXISTS gpuhub;

USE gpuhub;

-- requests 表
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
);

-- request_history 表
CREATE TABLE IF NOT EXISTS request_history (
    history_id INT AUTO_INCREMENT PRIMARY KEY,
    request_id VARCHAR(64) NOT NULL,
    from_status VARCHAR(32),
    to_status VARCHAR(32) NOT NULL,
    timestamp DATETIME NOT NULL,
    message TEXT,
    FOREIGN KEY (request_id) REFERENCES requests(request_id)
);

-- 完成
SELECT '✅ GPUHub 数据库初始化完成' AS message;