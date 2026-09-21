"""正式库与工作区库的连接管理。

两个独立 SQLite 文件，各自独立连接；跨库交接走提交信封协议
（见 workspace/service.py 与 memory/service.py），不假设跨库原子事务。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from . import config


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def formal():
    conn = _connect(config.FORMAL_DB)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def workspace():
    conn = _connect(config.WORKSPACE_DB)
    try:
        yield conn
    finally:
        conn.close()
