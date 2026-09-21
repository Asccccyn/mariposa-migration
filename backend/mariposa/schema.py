"""正式库与工作区库的 DDL 与 migration。

迁移链独立编号；结构即《01 工程执行文档》§5 最低数据模型的第一版落地，
状态字段按 §5.3 拆分（visibility / compression_state / date_confidence 各自独立）。
"""
from __future__ import annotations

from . import config, db

FORMAL_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE principals(
  principal_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('human','agent','system'))
);

CREATE TABLE client_bindings(
  binding_id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  principal_id TEXT NOT NULL REFERENCES principals(principal_id),
  entry_source TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE memories(
  memory_id TEXT PRIMARY KEY,
  current_version_no INTEGER NOT NULL,
  memory_date TEXT,
  date_confidence TEXT NOT NULL DEFAULT 'unknown'
    CHECK(date_confidence IN ('exact','inferred','unknown')),
  visibility TEXT NOT NULL DEFAULT 'active'
    CHECK(visibility IN ('active','hidden','archived')),
  compression_state TEXT NOT NULL DEFAULT 'full'
    CHECK(compression_state IN ('full','forgotten_summary')),
  pinned INTEGER NOT NULL DEFAULT 0,
  protected INTEGER NOT NULL DEFAULT 0,
  anchor INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_memories_date ON memories(memory_date);
CREATE INDEX idx_memories_state ON memories(visibility, compression_state);

CREATE TABLE memory_versions(
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  version_no INTEGER NOT NULL,
  representation TEXT NOT NULL CHECK(representation IN ('full','forgotten_summary')),
  hold_text TEXT,
  compressed_summary TEXT,
  why_remember TEXT,
  authored_by TEXT NOT NULL,
  confirmed_by TEXT,
  origin_kind TEXT NOT NULL
    CHECK(origin_kind IN ('initial_hold','forget_approval','restore')),
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, version_no)
);

CREATE TABLE retrieval_documents(
  memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id),
  memory_version_no INTEGER NOT NULL,
  projection_kind TEXT NOT NULL
    CHECK(projection_kind IN ('full','forgotten_summary')),
  search_text TEXT NOT NULL,
  search_text_hash TEXT NOT NULL,
  projection_revision TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_retrieval_version ON retrieval_documents(memory_id, memory_version_no);

CREATE VIRTUAL TABLE search_fts USING fts5(memory_id UNINDEXED, search_text);

CREATE TABLE proposal_envelopes(
  proposal_id TEXT PRIMARY KEY,
  proposal_revision INTEGER NOT NULL,
  proposal_hash TEXT NOT NULL,
  proposal_type TEXT NOT NULL,
  target_memory_id TEXT NOT NULL,
  base_memory_version INTEGER NOT NULL,
  submitted_by TEXT NOT NULL,
  submitted_at TEXT NOT NULL
);

CREATE TABLE proposal_resolutions(
  proposal_id TEXT PRIMARY KEY REFERENCES proposal_envelopes(proposal_id),
  decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','withdrawn')),
  decided_by TEXT NOT NULL,
  decided_binding TEXT NOT NULL,
  applied_memory_version INTEGER,
  decided_at TEXT NOT NULL
);

CREATE TABLE idempotency_records(
  principal_id TEXT NOT NULL,
  capability TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  result_ref TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(principal_id, capability, idempotency_key)
);

CREATE TABLE audit_events(
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  actor_principal TEXT NOT NULL,
  initiated_by TEXT,
  executor_kind TEXT,
  entry_source TEXT,
  resource_id TEXT,
  resource_version INTEGER,
  payload TEXT,
  correlation_id TEXT
);

CREATE TABLE events_outbox(
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  processed INTEGER NOT NULL DEFAULT 0
);
"""),
]

WORKSPACE_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE work_items(
  item_id TEXT PRIMARY KEY,
  item_type TEXT NOT NULL,
  target_memory_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('draft','submitted','approved_and_applied','rejected','withdrawn','stale','deferred')),
  current_revision INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolution_note TEXT
);

CREATE TABLE proposal_versions(
  proposal_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  submitted_at TEXT,
  PRIMARY KEY(proposal_id, revision)
);

CREATE TABLE worker_runs(
  run_id TEXT PRIMARY KEY,
  run_type TEXT NOT NULL,
  started_by TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  stats TEXT
);

CREATE TABLE workspace_audit(
  event_id TEXT PRIMARY KEY,
  occurred_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  item_id TEXT,
  detail TEXT
);
"""),
]


def migrate() -> None:
    config.ensure_dirs()
    with db.formal() as conn:
        _apply(conn, FORMAL_MIGRATIONS)
    with db.workspace() as conn:
        _apply(conn, WORKSPACE_MIGRATIONS)


def _apply(conn, migrations: list[tuple[int, str]]) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    for version, ddl in migrations:
        if version in applied:
            continue
        # executescript 会隐式提交已有事务，因此 BEGIN 必须写进脚本开头
        conn.executescript("BEGIN IMMEDIATE;\n" + ddl)
        try:
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, datetime('now'))",
                (version,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
