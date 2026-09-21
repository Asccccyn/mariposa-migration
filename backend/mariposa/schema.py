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
    (2, """
CREATE TABLE raw_conversations(
  id TEXT PRIMARY KEY,
  source_channel TEXT NOT NULL,
  external_id TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  coverage TEXT NOT NULL DEFAULT 'partial',
  created_at TEXT NOT NULL,
  UNIQUE(source_channel, external_id)
);

CREATE TABLE raw_messages(
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES raw_conversations(id),
  source_message_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','system')),
  speaker_id TEXT,
  body TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  provenance TEXT NOT NULL DEFAULT 'import',
  UNIQUE(conversation_id, source_message_id)
);
CREATE INDEX idx_raw_messages_time ON raw_messages(occurred_at, sequence);

CREATE TABLE quotes(
  id TEXT PRIMARY KEY,
  current_version_no INTEGER NOT NULL,
  said_at TEXT,
  said_at_confidence TEXT NOT NULL DEFAULT 'unknown'
    CHECK(said_at_confidence IN ('exact','inferred','unknown')),
  kept_by TEXT NOT NULL,
  withdrawn INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE quote_versions(
  quote_id TEXT NOT NULL REFERENCES quotes(id),
  version_no INTEGER NOT NULL,
  text TEXT NOT NULL,
  semantic_status TEXT NOT NULL DEFAULT 'no_source'
    CHECK(semantic_status IN ('equivalent','material_conflict','uncertain','no_source')),
  raw_ref TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(quote_id, version_no)
);

CREATE TABLE handoffs(
  id TEXT PRIMARY KEY,
  author TEXT NOT NULL,
  content TEXT NOT NULL,
  entry_source TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE plans(
  id TEXT PRIMARY KEY,
  current_version_no INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('planned','active','waiting','blocked','done','cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_plans_state ON plans(state);

CREATE TABLE plan_versions(
  plan_id TEXT NOT NULL REFERENCES plans(id),
  version_no INTEGER NOT NULL,
  title TEXT NOT NULL,
  content TEXT,
  state TEXT NOT NULL,
  starts_at TEXT,
  due_at TEXT,
  date_start TEXT,
  date_end TEXT,
  timezone TEXT,
  all_day INTEGER NOT NULL DEFAULT 0,
  weight TEXT,
  payload_hash TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(plan_id, version_no)
);

CREATE TABLE plan_memory_links(
  plan_id TEXT NOT NULL REFERENCES plans(id),
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  PRIMARY KEY(plan_id, memory_id)
);

CREATE TABLE activity_events(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN
    ('user_message','ui_activity','agent_or_system_activity')),
  principal TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX idx_activity_kind_time ON activity_events(kind, occurred_at);
"""),
    (3, """
CREATE TABLE letters(
  id TEXT PRIMARY KEY,
  current_version_no INTEGER NOT NULL,
  author TEXT NOT NULL,
  letter_date TEXT,
  lock_type TEXT NOT NULL DEFAULT 'none' CHECK(lock_type IN ('none','timed','locked')),
  unlock_date TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE letter_versions(
  letter_id TEXT NOT NULL REFERENCES letters(id),
  version_no INTEGER NOT NULL,
  content TEXT NOT NULL,
  edited_by TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(letter_id, version_no)
);

CREATE TABLE deletion_requests(
  id TEXT PRIMARY KEY,
  resource_id TEXT NOT NULL,
  resource_kind TEXT NOT NULL CHECK(resource_kind IN ('memory','letter')),
  action TEXT NOT NULL CHECK(action IN ('archive','delete')),
  human_reason TEXT NOT NULL,
  ai_reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN
    ('pending','approved','rejected','withdrawn','superseded')),
  submitted_by TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  local_date TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT
);
CREATE INDEX idx_deletion_resource ON deletion_requests(resource_id);
CREATE INDEX idx_deletion_status ON deletion_requests(status, local_date);
"""),
    (4, """
CREATE TABLE home(
  id INTEGER PRIMARY KEY CHECK(id = 1),
  current_version_no INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE home_versions(
  home_id INTEGER NOT NULL REFERENCES home(id),
  version_no INTEGER NOT NULL,
  content TEXT NOT NULL,
  edited_by TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(home_id, version_no)
);

CREATE TABLE self_entries(
  id TEXT PRIMARY KEY,
  aspect TEXT NOT NULL DEFAULT '',
  current_version_no INTEGER NOT NULL,
  review_state TEXT NOT NULL DEFAULT 'pending'
    CHECK(review_state IN ('pending','reviewed','retired')),
  written_at TEXT NOT NULL,
  review_available_on TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE self_versions(
  self_id TEXT NOT NULL REFERENCES self_entries(id),
  version_no INTEGER NOT NULL,
  content TEXT NOT NULL,
  written_by TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(self_id, version_no)
);

CREATE TABLE diary_entries(
  id TEXT PRIMARY KEY,
  current_version_no INTEGER NOT NULL,
  author TEXT NOT NULL,
  covers_from TEXT,
  covers_to TEXT,
  hidden INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_diary_covers ON diary_entries(covers_from, covers_to);

CREATE TABLE diary_versions(
  diary_id TEXT NOT NULL REFERENCES diary_entries(id),
  version_no INTEGER NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  edited_by TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(diary_id, version_no)
);

CREATE TABLE memory_tags(
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  namespace TEXT NOT NULL,
  tag TEXT NOT NULL,
  whose TEXT NOT NULL CHECK(whose IN ('jiaming','qiaosheng')),
  confidence TEXT NOT NULL DEFAULT 'human',
  created_by TEXT NOT NULL,
  PRIMARY KEY(memory_id, namespace, tag, whose)
);
CREATE INDEX idx_memory_tags ON memory_tags(namespace, tag, whose);

CREATE TABLE bootstrap_snapshots(
  snapshot_id TEXT PRIMARY KEY,
  state_hash TEXT NOT NULL,
  profile TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""),
    (5, """
CREATE TABLE memory_relations(
  from_memory TEXT NOT NULL REFERENCES memories(memory_id),
  to_memory TEXT NOT NULL REFERENCES memories(memory_id),
  relation_type TEXT NOT NULL,
  custom_label TEXT,
  reverse_label TEXT,
  confidence TEXT NOT NULL DEFAULT 'human',
  active INTEGER NOT NULL DEFAULT 1,
  version INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(from_memory, to_memory, relation_type)
);

CREATE TABLE memory_raw_refs(
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  conversation_id TEXT NOT NULL,
  message_from TEXT NOT NULL,
  message_to TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  bind_confidence TEXT NOT NULL DEFAULT 'exact'
    CHECK(bind_confidence IN ('exact','high','low','revoked')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, conversation_id, message_from)
);

CREATE TABLE provisional_sources(
  id TEXT PRIMARY KEY,
  memory_id TEXT,
  reported_by TEXT NOT NULL,
  reported_at TEXT NOT NULL,
  fragment TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unbound'
    CHECK(status IN ('unbound','bound','dismissed'))
);

CREATE TABLE reminders(
  id TEXT PRIMARY KEY,
  principal TEXT NOT NULL,
  title TEXT NOT NULL,
  note TEXT,
  remind_at TEXT NOT NULL,
  timezone TEXT,
  status TEXT NOT NULL DEFAULT 'scheduled'
    CHECK(status IN ('scheduled','fired','cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_reminders_time ON reminders(status, remind_at);

CREATE TABLE media_objects(
  content_hash TEXT PRIMARY KEY,
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  storage_key TEXT NOT NULL UNIQUE,
  owned_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE moments(
  id TEXT PRIMARY KEY,
  current_version_no INTEGER NOT NULL,
  author TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'post' CHECK(kind IN ('post','group_archive')),
  visibility TEXT NOT NULL DEFAULT 'normal',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE moment_versions(
  moment_id TEXT NOT NULL REFERENCES moments(id),
  version_no INTEGER NOT NULL,
  content TEXT NOT NULL,
  media_hash TEXT,
  edited_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(moment_id, version_no)
);

CREATE TABLE rejection_suppression(
  target_memory_id TEXT NOT NULL,
  suppressed_until TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(target_memory_id, suppressed_until)
);
"""),
    (6, """
ALTER TABLE memories ADD COLUMN source_state TEXT NOT NULL DEFAULT 'raw_pending'
  CHECK(source_state IN ('raw_pending','bound','conflict'));
"""),
    (7, """
CREATE TABLE moment_comments(
  id TEXT PRIMARY KEY,
  moment_id TEXT NOT NULL REFERENCES moments(id),
  author TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_moment_comments ON moment_comments(moment_id);

CREATE TABLE moment_reactions(
  moment_id TEXT NOT NULL REFERENCES moments(id),
  principal TEXT NOT NULL,
  reaction TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(moment_id, principal)
);
"""),
    (8, """
CREATE TABLE memory_meanings(
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  layer_no INTEGER NOT NULL,
  content TEXT NOT NULL,
  written_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, layer_no)
);

CREATE TABLE stickers(
  content_hash TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  mime TEXT NOT NULL,
  storage_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE import_jobs(
  id TEXT PRIMARY KEY,
  source_channel TEXT NOT NULL,
  external_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'prepared'
    CHECK(status IN ('prepared','completed','failed')),
  message_count INTEGER NOT NULL DEFAULT 0,
  parser_version TEXT NOT NULL DEFAULT 'raw_json_v1',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""),
    (9, """
ALTER TABLE letters ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
"""),
]

WORKSPACE_MIGRATIONS: list[tuple[int, str]] = [
    (2, """
CREATE TABLE IF NOT EXISTS workspace_task_leases(
  lease_id TEXT PRIMARY KEY,
  task_key TEXT NOT NULL,
  claimed_by TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_task_leases ON workspace_task_leases(task_key, released);
"""),
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
