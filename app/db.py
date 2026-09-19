"""SQLite 存储层：连接管理与表结构初始化。"""
from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = "calibration.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL UNIQUE,          -- 观测时点（Unix 秒）
    reference     REAL NOT NULL,                 -- 参考值（真值）
    tolerance     REAL NOT NULL,                 -- 请求容差
    group_median  REAL NOT NULL,                 -- 读数组中位数
    bias          REAL NOT NULL,                 -- 校准偏差 = reference - 有效读数中位数
    revision      INTEGER NOT NULL DEFAULT 1,    -- 当前版本号，每次成功修订 +1
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    value          REAL NOT NULL,                -- 原始读数，永不修改
    is_outlier     INTEGER NOT NULL DEFAULT 0    -- 是否异常读数
);

-- 相邻观测时点构成的插值区间，新增观测后仅重算受影响的相邻区间
CREATE TABLE IF NOT EXISTS intervals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    t_start      REAL NOT NULL,
    t_end        REAL NOT NULL,
    bias_start   REAL NOT NULL,
    bias_end     REAL NOT NULL,
    obs_start_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    obs_end_id   INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    UNIQUE (t_start, t_end)
);

CREATE INDEX IF NOT EXISTS idx_readings_obs ON readings(observation_id);
CREATE INDEX IF NOT EXISTS idx_intervals_span ON intervals(t_start, t_end);

-- 每次成功修订前保存的旧值快照（revision = 该次修订前的版本号）
CREATE TABLE IF NOT EXISTS revisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    revision       INTEGER NOT NULL,             -- 修订前版本
    changed_at     REAL NOT NULL,                -- 修订发生时间（Unix 秒）
    old_ts         REAL NOT NULL,                -- 修订前观测时点
    old_reference  REAL NOT NULL,
    old_tolerance  REAL NOT NULL,
    old_bias       REAL NOT NULL,                -- 修订前校准偏差
    old_readings   TEXT NOT NULL,                -- 修订前原始读数（JSON 数组，保持顺序）
    UNIQUE (observation_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_revisions_obs ON revisions(observation_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """为建表早于版本功能的旧库补齐 revision 列（新库建表时已含）。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(observations)")}
    if "revision" not in cols:
        conn.execute("ALTER TABLE observations ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")


def db_path() -> str:
    return os.environ.get("CALIB_DB_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
