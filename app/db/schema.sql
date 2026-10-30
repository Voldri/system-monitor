-- DDL схема бази даних SQLite для System Monitor

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    cpu_total REAL NOT NULL,
    cpu_max REAL NOT NULL,
    cpu_cores TEXT NOT NULL,
    ram_used_bytes INTEGER NOT NULL,
    ram_total_bytes INTEGER NOT NULL,
    ram_percent REAL NOT NULL,
    ram_max REAL NOT NULL,
    swap_used_bytes INTEGER NOT NULL,
    swap_total_bytes INTEGER NOT NULL,
    disk_read_bytes_sec REAL NOT NULL,
    disk_write_bytes_sec REAL NOT NULL,
    net_sent_bytes_sec REAL NOT NULL,
    net_recv_bytes_sec REAL NOT NULL,
    disks_info TEXT NOT NULL,
    is_aggregated INTEGER DEFAULT 0,
    gpu_percent REAL DEFAULT NULL,
    gpu_max REAL DEFAULT NULL,
    gpu_mem_used_bytes INTEGER DEFAULT NULL,
    gpu_mem_total_bytes INTEGER DEFAULT NULL,
    gpu_temp_c REAL DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_ts_agg ON metrics(timestamp, is_aggregated);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    resolved_at INTEGER DEFAULT NULL,
    metric_name TEXT NOT NULL,
    rule_condition TEXT NOT NULL,
    trigger_value REAL NOT NULL,
    threshold_value REAL NOT NULL,
    severity TEXT NOT NULL,
    culprits_summary TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);

CREATE TABLE IF NOT EXISTS process_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    alert_id INTEGER DEFAULT NULL,
    pid INTEGER NOT NULL,
    name TEXT NOT NULL,
    cpu_percent REAL NOT NULL,
    memory_percent REAL NOT NULL,
    memory_rss_bytes INTEGER NOT NULL,
    status TEXT DEFAULT 'running',
    FOREIGN KEY(alert_id) REFERENCES alerts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_proc_timestamp ON process_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_proc_alert_id ON process_snapshots(alert_id);

CREATE TABLE IF NOT EXISTS ai_advice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    mode TEXT NOT NULL,
    source TEXT NOT NULL,
    user_query TEXT DEFAULT NULL,
    metrics_summary TEXT NOT NULL,
    advice_text TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_advice_timestamp ON ai_advice(timestamp);
