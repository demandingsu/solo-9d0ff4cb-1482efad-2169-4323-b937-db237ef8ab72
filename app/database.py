"""SQLite 连接与建表。

数据库路径由环境变量 GLYPH_DB_PATH 决定，默认 data/glyph.db。
所有上传与启用动作都在显式 BEGIN IMMEDIATE 事务内完成，写入失败即回滚：
待补记录随表持久化，进程重启后仍可接着补传。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("GLYPH_DB_PATH", str(BASE_DIR / "data" / "glyph.db"))

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS volumes (
        volume_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS drafts (
        volume_id    INTEGER NOT NULL REFERENCES volumes(volume_id),
        draft_no     INTEGER NOT NULL CHECK (draft_no > 0),
        status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'active', 'superseded')),
        has_list     INTEGER NOT NULL DEFAULT 0,
        has_mapping  INTEGER NOT NULL DEFAULT 0,
        list_codes   TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        activated_at TEXT,
        PRIMARY KEY (volume_id, draft_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS draft_list_codes (
        volume_id    INTEGER NOT NULL,
        draft_no     INTEGER NOT NULL,
        code         TEXT NOT NULL,
        source_order INTEGER NOT NULL,
        PRIMARY KEY (volume_id, draft_no, code),
        FOREIGN KEY (volume_id, draft_no) REFERENCES drafts(volume_id, draft_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS draft_mappings (
        volume_id    INTEGER NOT NULL,
        draft_no     INTEGER NOT NULL,
        code         TEXT NOT NULL,
        glyph_char   TEXT NOT NULL,
        source_order INTEGER NOT NULL,
        PRIMARY KEY (volume_id, draft_no, code),
        UNIQUE (volume_id, draft_no, glyph_char),
        FOREIGN KEY (volume_id, draft_no) REFERENCES drafts(volume_id, draft_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_drafts_volume ON drafts(volume_id, status)",
    """
    CREATE TABLE IF NOT EXISTS legacy_tables (
        volume_id    INTEGER PRIMARY KEY REFERENCES volumes(volume_id),
        content      TEXT NOT NULL,
        uploaded_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
]


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
    finally:
        conn.close()
