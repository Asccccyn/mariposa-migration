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
    CHECK(visibility IN ('active','hidden')),
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




CREATE TABLE media_objects(
  content_hash TEXT PRIMARY KEY,
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  storage_key TEXT NOT NULL UNIQUE,
  owned_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);



"""),
    (6, """
ALTER TABLE memories ADD COLUMN source_state TEXT NOT NULL DEFAULT 'raw_pending'
  CHECK(source_state IN ('raw_pending','bound','conflict'));
"""),
    (7, """

"""),
    (8, """


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
-- （letters archived 列随 v2.0 零残留清理移除：letters 表已随
--  migration 24 退役，无部署库需要此升级步骤）
"""),
    (10, """
CREATE TABLE memory_reengagements(
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  evidence_kind TEXT NOT NULL,
  evidence_ref TEXT,
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  recorded_by TEXT NOT NULL,
  PRIMARY KEY(memory_id, occurred_at, evidence_kind)
);
CREATE INDEX idx_reengage ON memory_reengagements(memory_id, occurred_at);

CREATE TABLE migration_id_map(
  legacy_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  new_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  migrated_at TEXT NOT NULL,
  PRIMARY KEY(legacy_id, source_type)
);
"""),
    (11, """
-- ===== v2 分层模型（spec_v2 §5）=====

CREATE TABLE memory_categories(
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  category TEXT NOT NULL CHECK(category IN
    ('daily','milestone','sad','sweet','date','plan','sex','anniversary',
     'reloplay')),
  added_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, category)
);
CREATE INDEX idx_categories_category ON memory_categories(category);

CREATE TABLE memory_moods(
  memory_id TEXT PRIMARY KEY REFERENCES memories(memory_id),
  mood_text TEXT,
  author TEXT NOT NULL CHECK(author IN ('jiaming')),
  captured_session TEXT,
  captured_at TEXT NOT NULL,
  evidence_state TEXT NOT NULL CHECK(evidence_state IN
    ('contemporaneous','window_verified','absent'))
);

CREATE TABLE memory_mood_tags(
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  tag TEXT NOT NULL,
  PRIMARY KEY(memory_id, tag)
);
CREATE INDEX idx_mood_tags_tag ON memory_mood_tags(tag);

CREATE TABLE memory_our_words(
  word_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  ordinal INTEGER NOT NULL,
  speaker TEXT NOT NULL CHECK(speaker IN ('jiaming','qiaosheng')),
  text TEXT NOT NULL,
  expression_kind TEXT NOT NULL DEFAULT 'unspecified'
    CHECK(expression_kind IN ('verbatim','paraphrase','unspecified')),
  source_ref TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(memory_id, ordinal)
);

CREATE TABLE memory_view_receipts(
  receipt_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  representation_version INTEGER NOT NULL,
  confirm_key TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  confirmed_at TEXT,
  UNIQUE(principal_id, confirm_key)
);
CREATE INDEX idx_view_receipts_memory ON memory_view_receipts(memory_id, principal_id);

CREATE TABLE memory_recollections(
  recollection_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  author TEXT NOT NULL CHECK(author IN ('jiaming','qiaosheng')),
  text TEXT NOT NULL,
  view_receipt TEXT,
  supersedes TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  written_at TEXT NOT NULL
);
CREATE INDEX idx_recollections_memory ON memory_recollections(memory_id, written_at);

CREATE TABLE i_documents(
  doc_id TEXT PRIMARY KEY,
  current_version_no INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE i_versions(
  doc_id TEXT NOT NULL REFERENCES i_documents(doc_id),
  version_no INTEGER NOT NULL,
  content TEXT NOT NULL,
  authored_by TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(doc_id, version_no)
);

CREATE TABLE i_suggestions(
  suggestion_id TEXT PRIMARY KEY,
  suggested_by TEXT NOT NULL CHECK(suggested_by IN ('qiaosheng')),
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN ('open','accepted','dismissed')),
  created_at TEXT NOT NULL
);

ALTER TABLE memories ADD COLUMN held_at TEXT;
ALTER TABLE memories ADD COLUMN held_at_confidence TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE memories ADD COLUMN creation_mode TEXT NOT NULL DEFAULT 'legacy_unknown'
  CHECK(creation_mode IN ('contemporaneous','retrospective','legacy_unknown'));
ALTER TABLE memories ADD COLUMN representation_state INTEGER NOT NULL DEFAULT 1;

ALTER TABLE memory_versions ADD COLUMN original_title TEXT;
ALTER TABLE memory_versions ADD COLUMN event_text TEXT;
ALTER TABLE memory_versions ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE plans ADD COLUMN completed_at TEXT;
ALTER TABLE plans ADD COLUMN abandoned_at TEXT;
ALTER TABLE plans ADD COLUMN terminal_date TEXT;
ALTER TABLE plans ADD COLUMN due_date TEXT;
ALTER TABLE plans ADD COLUMN policy_timezone TEXT;
ALTER TABLE plans ADD COLUMN terminal_revision INTEGER NOT NULL DEFAULT 0;

CREATE TABLE anniversary_definitions(
  definition_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  rule TEXT NOT NULL DEFAULT 'yearly',
  rule_version TEXT NOT NULL DEFAULT 'anniversary_rule_v1',
  memory_id TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE anniversary_occurrences(
  occurrence_id TEXT PRIMARY KEY,
  definition_id TEXT NOT NULL REFERENCES anniversary_definitions(definition_id),
  occurrence_date TEXT NOT NULL,
  UNIQUE(definition_id, occurrence_date)
);
CREATE INDEX idx_anniv_occ_date ON anniversary_occurrences(occurrence_date);
"""),
    (12, """
ALTER TABLE memories ADD COLUMN occurred_start TEXT;
ALTER TABLE memories ADD COLUMN occurred_end TEXT;
ALTER TABLE bootstrap_snapshots ADD COLUMN business_date TEXT;
ALTER TABLE retrieval_documents ADD COLUMN whitelist_body TEXT;
"""),
    (13, """
CREATE TABLE mariposa_db_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- words 派生索引（v1.4 §7.3/§10.3）：可重建的检索辅助表，不是正式正文。
-- 绑定 word 正文版本与所属 memory 当前表示；遗忘/restore/话语变更/撤权
-- 由读取时校验兜底（words.retrieval 读取前重查当前表示），派生行过期
-- 即失效，不承担正式语义。
CREATE TABLE words_search_docs(
  word_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL,
  memory_version_no INTEGER NOT NULL,
  memory_compression_state TEXT NOT NULL,
  text_norm TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  projection_version TEXT NOT NULL,
  built_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE words_fts USING fts5(word_id UNINDEXED, text_norm);
"""),
    (14, """
-- ===== Source Layer（原文层，2026-09-27）=====
-- 与 v1 raw_*（合成/导出导入）相互独立；原文层保存 provider 原始导出的
-- 标准化副本，母本在 Raw Archive（runtime/source/raw/，不进库、不进 git）。
-- 正文 text 只存 human/assistant 的用户可见文本；thinking/tool 只留标志
-- 与 content_json 证据，不混入正文（实施规格 §5）。

CREATE TABLE source_import_batches(
  batch_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  original_filename TEXT NOT NULL,
  original_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  raw_path TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  import_started_at TEXT NOT NULL,
  import_finished_at TEXT,
  error TEXT,
  stats TEXT NOT NULL DEFAULT '{}',
  imported_by TEXT NOT NULL,
  UNIQUE(provider, sha256)
);

CREATE TABLE source_conversations(
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  provider_conversation_id TEXT NOT NULL,
  title TEXT,
  created_at TEXT,
  updated_at TEXT,
  message_count INTEGER NOT NULL DEFAULT 0,
  first_message_at TEXT,
  last_message_at TEXT,
  first_import_batch_id TEXT NOT NULL,
  last_import_batch_id TEXT NOT NULL,
  UNIQUE(provider, provider_conversation_id)
);
CREATE INDEX idx_source_conv_time ON source_conversations(updated_at);

CREATE TABLE source_messages(
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES source_conversations(id),
  provider TEXT NOT NULL,
  provider_conversation_id TEXT NOT NULL,
  provider_message_id TEXT NOT NULL,
  id_synthetic INTEGER NOT NULL DEFAULT 0,
  parent_provider_message_id TEXT,
  raw_sender TEXT,
  normalized_sender TEXT NOT NULL
    CHECK(normalized_sender IN ('human','assistant','system','tool','unknown')),
  speaker TEXT CHECK(speaker IN ('qiaosheng','jiaming') OR speaker IS NULL),
  created_at TEXT,
  updated_at TEXT,
  occurred_date TEXT,
  text TEXT NOT NULL DEFAULT '',
  content_json TEXT,
  attachments TEXT NOT NULL DEFAULT '[]',
  has_thinking INTEGER NOT NULL DEFAULT 0,
  has_tool_content INTEGER NOT NULL DEFAULT 0,
  sequence INTEGER NOT NULL,
  import_batch_id TEXT NOT NULL,
  UNIQUE(provider, provider_message_id)
);
CREATE INDEX idx_source_msg_conv_seq ON source_messages(conversation_id, sequence);
CREATE INDEX idx_source_msg_date ON source_messages(occurred_date);
CREATE INDEX idx_source_msg_sender ON source_messages(normalized_sender);
CREATE INDEX idx_source_msg_created ON source_messages(created_at);

-- 原文检索投影（可重建，不是正本）：仅 human/assistant 的 text 进入。
CREATE TABLE source_search_docs(
  message_id TEXT PRIMARY KEY REFERENCES source_messages(id),
  provider_message_id TEXT NOT NULL,
  text_norm TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  projection_version TEXT NOT NULL,
  built_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE source_fts USING fts5(message_id UNINDEXED, text_norm);

-- Semantic Source Binding：一条 Memory 可绑多个 source range（多行）。
-- 默认 message boundary；句内片段才用 char offset（可空）。
CREATE TABLE memory_source_bindings(
  binding_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  conversation_id TEXT NOT NULL REFERENCES source_conversations(id),
  start_message_id TEXT NOT NULL REFERENCES source_messages(id),
  end_message_id TEXT NOT NULL REFERENCES source_messages(id),
  start_char_offset INTEGER,
  end_char_offset INTEGER,
  bind_confidence TEXT NOT NULL DEFAULT 'exact'
    CHECK(bind_confidence IN ('exact','high','low','revoked')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_msb_memory ON memory_source_bindings(memory_id, bind_confidence);
CREATE INDEX idx_msb_conv ON memory_source_bindings(conversation_id);
"""),
    (15, """
-- ===== Source Layer 定点补修（2026-09-27 v1.1 复核；SL-05/07/10）=====

-- 发布门禁：只有批次完整校验通过后才置 1；失败批次的新增消息默认不可见
ALTER TABLE source_messages ADD COLUMN published INTEGER NOT NULL DEFAULT 0;

-- 消息规范化内容身份：同 UUID 跨导出内容变化的判定依据
ALTER TABLE source_messages ADD COLUMN content_hash TEXT;

-- 会话快照：一次导入对一个会话的一次观察（元数据不再永久停留首见）
CREATE TABLE source_conversation_snapshots(
  snapshot_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES source_conversations(id),
  batch_id TEXT NOT NULL REFERENCES source_import_batches(batch_id),
  title TEXT,
  observed_created_at TEXT,
  observed_updated_at TEXT,
  message_count INTEGER NOT NULL DEFAULT 0,
  sequence_conflicts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(conversation_id, batch_id)
);
CREATE INDEX idx_scs_conv ON source_conversation_snapshots(conversation_id);

-- 快照成员：sequence 只在快照内解释，不混用多次导出的数组序（SL-07）
CREATE TABLE source_snapshot_members(
  snapshot_id TEXT NOT NULL REFERENCES source_conversation_snapshots(snapshot_id),
  provider_message_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, provider_message_id)
);
CREATE INDEX idx_ssm_seq ON source_snapshot_members(snapshot_id, sequence);

-- 不可变消息版本：同 UUID 不同内容各留一份，永不覆盖/丢弃（SL-07）
CREATE TABLE source_message_versions(
  version_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  provider_message_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  observed_batch_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  UNIQUE(provider, provider_message_id, content_hash)
);
CREATE INDEX idx_smv_msg ON source_message_versions(provider, provider_message_id);

-- 绑定固定证据版本：记录绑定时的正文 hash，读取时校验防漂移（SL-05）
ALTER TABLE memory_source_bindings ADD COLUMN start_content_hash TEXT;
ALTER TABLE memory_source_bindings ADD COLUMN end_content_hash TEXT;
"""),
    (16, """
-- ===== v1.7（2026-09-28）：删除遗忘/压缩/审查链；九分类；明开回温 =====
-- 新 fresh 库不会创建这些结构（历史 migration DDL 已摘除）；本迁移
-- 只对旧库执行 DROP。历史数据保全走一次性离线迁移（scripts/migrations/
-- offline_v15/，另行授权执行），不在在线链路复活任何摘要实现。

DROP TABLE IF EXISTS forgetting_due_queue;
DROP TABLE IF EXISTS memory_summary_versions;
DROP TABLE IF EXISTS memory_retention;
DROP TABLE IF EXISTS rejection_suppression;
DROP TABLE IF EXISTS review_delegations;

-- 明开回温事实列（v1.7 §5.4：确认时刻，取 max 防倒退）
ALTER TABLE memories ADD COLUMN last_explicit_open_at TEXT;
"""),
    (17, """
-- ===== v1.7 P2：作者「留」（keep_wide）——回忆写入时显式选择，谁留谁撤 =====
-- 理由就是回忆本身，不另设 reason 字段；keep 指向选择时的回忆版本，
-- 回忆后续修订不改指向。两位作者独立标记 OR 生效。
CREATE TABLE memory_keeps(
  mark_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  owner TEXT NOT NULL CHECK(owner IN ('qiaosheng','jiaming')),
  recollection_id TEXT NOT NULL,
  recollection_version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX idx_keeps_memory ON memory_keeps(memory_id, owner);
"""),
    (18, """
-- ===== v1.7 P3：分字段投影（§3.1：event/title/words 独立派生索引）=====
-- 阶段过滤（WIDE 6 / MID 5 / CORE 4）按 field_kind 精确去留；
-- mood_text/回忆/keep 理由永不入索引（字段来源隔离，不是全局拉黑）。
CREATE TABLE field_search_docs(
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  field_kind TEXT NOT NULL CHECK(field_kind IN
    ('original_title', 'event_text', 'our_words')),
  text_norm TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  projection_version TEXT NOT NULL,
  built_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, field_kind)
);
CREATE VIRTUAL TABLE field_fts USING fts5(
  memory_id UNINDEXED, field_kind UNINDEXED, text_norm);
"""),
    (19, """
-- ===== v1.7 A08：九分类 CHECK 增量迁移（旧库升级路径）=====
-- migration 11 的 DDL 只影响 fresh 库；已有安装需重建 memory_categories
-- 以启用 reloplay。数据逐行保留。
CREATE TABLE memory_categories_new(
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  category TEXT NOT NULL CHECK(category IN
    ('daily','milestone','sad','sweet','date','plan','sex','anniversary',
     'reloplay')),
  added_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(memory_id, category)
);
INSERT INTO memory_categories_new SELECT * FROM memory_categories;
DROP TABLE memory_categories;
ALTER TABLE memory_categories_new RENAME TO memory_categories;
CREATE INDEX idx_categories_category ON memory_categories(category);
"""),
    (20, """
-- ===== v1.7 A13：旧 Source 数据补偿回填 =====
-- migration 15 之前完成的 Source 批次没有 published 列语义；升级后
-- 其消息被误置不可见。凡属 completed 批次的消息回填 published=1。
-- content_hash 留 NULL，由下一次同文件导入的接管路径回填正式身份。
UPDATE source_messages SET published=1
 WHERE published=0 AND import_batch_id IN (
   SELECT batch_id FROM source_import_batches WHERE status='completed');
"""),
    (21, """
-- ===== v1.7 F9：死表清理（遗忘提案流孤儿结构）=====
DROP TABLE IF EXISTS proposal_resolutions;
DROP TABLE IF EXISTS proposal_envelopes;
DROP TABLE IF EXISTS workspace_audit;
"""),
    (22, """
-- ===== I 条目级版本历史：当前态唯一生效，旧版仅显式查阅 =====
CREATE TABLE i_items(
  item_id TEXT PRIMARY KEY,
  position INTEGER NOT NULL,
  current_revision INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE i_item_revisions(
  item_id TEXT NOT NULL REFERENCES i_items(item_id),
  revision INTEGER NOT NULL,
  content TEXT NOT NULL,
  authored_by TEXT NOT NULL,
  change_type TEXT NOT NULL CHECK(change_type IN ('create','revise','restore')),
  based_on_revision INTEGER,
  restored_from_revision INTEGER,
  informed_by_revision INTEGER,
  change_reason TEXT,
  payload_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(item_id, revision)
);

CREATE TABLE i_revision_memory_relations(
  item_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  relation_type TEXT NOT NULL CHECK(relation_type IN
    ('changed_because_of','clarified_by','informed_by','related')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(item_id, revision, memory_id, relation_type),
  FOREIGN KEY(item_id, revision) REFERENCES i_item_revisions(item_id, revision)
);
CREATE INDEX idx_i_revision_memory
  ON i_revision_memory_relations(memory_id, item_id, revision);

-- 已经存在的正式 I 正本安全迁成一个 i_main 条目；不碰旧 Self/Home。
INSERT INTO i_items(item_id, position, current_revision, created_at, updated_at)
SELECT 'i_main', 0, d.current_version_no,
       COALESCE((SELECT MIN(v.created_at) FROM i_versions v
                 WHERE v.doc_id=d.doc_id), d.updated_at),
       d.updated_at
  FROM i_documents d
 WHERE d.doc_id='i_main' AND d.current_version_no > 0;

INSERT INTO i_item_revisions(
  item_id, revision, content, authored_by, change_type, based_on_revision,
  restored_from_revision, informed_by_revision, change_reason,
  payload_hash, created_at)
SELECT 'i_main', v.version_no, v.content, v.authored_by,
       CASE WHEN v.version_no=1 THEN 'create' ELSE 'revise' END,
       CASE WHEN v.version_no=1 THEN NULL ELSE v.version_no-1 END,
       NULL, NULL, NULL, v.payload_hash, v.created_at
  FROM i_versions v
 WHERE v.doc_id='i_main';
"""),
    (23, """
-- ===== P1-03：Source 导入租约 fencing token =====
-- 每次认领/接管生成新 token；所有对批次的写入（会话级解析事务、
-- 发布事务、失败清场、metadata 定稿）必须持有当前 token，过期
-- worker 的任何写入（含失败清场）都被拒绝，不能破坏接管方的
-- 成功证据。
ALTER TABLE source_import_batches ADD COLUMN lease_token TEXT;
"""),
    (24, """
-- ===== 信件拆分（2026-10-01）：letters 移出 mariposa（独立项目另行开发）=====
-- 旧库中的信件行随表移除；如将来独立信件项目需要旧数据，从备份迁移。
DROP TABLE IF EXISTS letter_versions;
DROP TABLE IF EXISTS letters;
"""),
    (25, """
-- ===== 旧模块退役（2026-10-01，legacy-removal D02-D13）=====
-- 生产库取证：下列表 row_count 全部为 0（2026-10-01 只读 COUNT），
-- 按 §7 规则安全 DROP；fresh 库已不再创建（基础 DDL 同批移除）。
DROP TABLE IF EXISTS moment_reactions;
DROP TABLE IF EXISTS moment_comments;
DROP TABLE IF EXISTS moment_versions;
DROP TABLE IF EXISTS moments;
DROP TABLE IF EXISTS stickers;
DROP TABLE IF EXISTS quote_versions;
DROP TABLE IF EXISTS quotes;
DROP TABLE IF EXISTS memory_meanings;
DROP TABLE IF EXISTS diary_versions;
DROP TABLE IF EXISTS diary_entries;
DROP TABLE IF EXISTS self_versions;
DROP TABLE IF EXISTS self_entries;
DROP TABLE IF EXISTS home_versions;
DROP TABLE IF EXISTS home;
DROP TABLE IF EXISTS reminders;
DROP TABLE IF EXISTS memory_raw_refs;
DROP TABLE IF EXISTS provisional_sources;
DROP TABLE IF EXISTS raw_messages;
DROP TABLE IF EXISTS raw_conversations;
-- 旧审批体系残留（当前 schema 无 CREATE，部署库可能存在）
DROP TABLE IF EXISTS proposal_envelopes;
DROP TABLE IF EXISTS proposal_resolutions;
DROP TABLE IF EXISTS review_delegations;
DROP TABLE IF EXISTS rejection_suppression;
"""),
(26, """
-- ===== Relation/Deletion 语义收口（2026-10-01，规格 v2.0）=====
-- 表重建涉及被 FK 引用的母表：事务内延迟 FK 校验至提交点
PRAGMA defer_foreign_keys=ON;
-- ① 关系实例身份 + 软删/无效字段退役（历史迁 relation_corrections）
CREATE TABLE relation_corrections(
  correction_id TEXT PRIMARY KEY,
  domain TEXT NOT NULL CHECK(domain IN
    ('memory_relation','i_revision_relation','source_binding',
     'plan_link','word_source')),
  original_instance_id TEXT NOT NULL,
  endpoint_a TEXT NOT NULL,
  endpoint_b TEXT,
  original_meta TEXT,
  original_created_by TEXT,
  original_created_at TEXT,
  corrected_by TEXT NOT NULL,
  corrected_at TEXT NOT NULL,
  reason_code TEXT NOT NULL DEFAULT 'binding_error',
  note TEXT,
  replacement_instance_id TEXT
);

CREATE TABLE memory_relations_v2(
  relation_id TEXT PRIMARY KEY,
  from_memory TEXT NOT NULL REFERENCES memories(memory_id),
  to_memory TEXT NOT NULL REFERENCES memories(memory_id),
  relation_type TEXT NOT NULL,
  custom_label TEXT,
  reverse_label TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(from_memory, to_memory, relation_type)
);
INSERT INTO memory_relations_v2(relation_id, from_memory, to_memory,
  relation_type, custom_label, reverse_label, created_by, created_at)
SELECT 'rel_' || lower(hex(randomblob(12))), from_memory, to_memory,
  relation_type, custom_label, reverse_label, created_by, created_at
  FROM memory_relations WHERE active=1;
INSERT INTO relation_corrections(correction_id, domain,
  original_instance_id, endpoint_a, endpoint_b, original_meta,
  original_created_by, original_created_at, corrected_by, corrected_at,
  reason_code, note)
SELECT 'corr_' || lower(hex(randomblob(12))), 'memory_relation',
  'legacy:' || from_memory || '>' || to_memory || ':' || relation_type,
  from_memory, to_memory,
  json_object('relation_type', relation_type, 'legacy_soft_delete', 1),
  created_by, created_at, 'legacy_migration', datetime('now'),
  'binding_error', 'legacy 未记录纠错人（迁移保留未知）'
  FROM memory_relations WHERE active=0;
DROP TABLE memory_relations;
ALTER TABLE memory_relations_v2 RENAME TO memory_relations;

-- ② I 修订关系：实例身份 + related→related_to（表重建换 CHECK）
CREATE TABLE i_revision_memory_relations_v2(
  relation_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  relation_type TEXT NOT NULL CHECK(relation_type IN
    ('changed_because_of','clarified_by','informed_by','related_to')),
  created_at TEXT NOT NULL,
  UNIQUE(item_id, revision, memory_id, relation_type),
  FOREIGN KEY(item_id, revision) REFERENCES i_item_revisions(item_id, revision)
);
INSERT INTO i_revision_memory_relations_v2(relation_id, item_id, revision,
  memory_id, relation_type, created_at)
SELECT 'irr_' || lower(hex(randomblob(12))), item_id, revision, memory_id,
  CASE WHEN relation_type='related' THEN 'related_to' ELSE relation_type END,
  created_at FROM i_revision_memory_relations;
DROP TABLE i_revision_memory_relations;
ALTER TABLE i_revision_memory_relations_v2
  RENAME TO i_revision_memory_relations;
CREATE INDEX idx_i_revision_memory
  ON i_revision_memory_relations(memory_id, item_id, revision);

-- ③ plan 链接：实例身份
CREATE TABLE plan_memory_links_v2(
  link_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES plans(id),
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  UNIQUE(plan_id, memory_id)
);
INSERT INTO plan_memory_links_v2(link_id, plan_id, memory_id)
SELECT 'pml_' || lower(hex(randomblob(12))), plan_id, memory_id
  FROM plan_memory_links;
DROP TABLE plan_memory_links;
ALTER TABLE plan_memory_links_v2 RENAME TO plan_memory_links;

-- ④ Source 绑定：revoked 行迁纠错历史，CHECK 收口
CREATE TABLE memory_source_bindings_v2(
  binding_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(memory_id),
  conversation_id TEXT NOT NULL REFERENCES source_conversations(id),
  start_message_id TEXT NOT NULL REFERENCES source_messages(id),
  end_message_id TEXT NOT NULL REFERENCES source_messages(id),
  start_char_offset INTEGER,
  end_char_offset INTEGER,
  bind_confidence TEXT NOT NULL DEFAULT 'exact'
    CHECK(bind_confidence IN ('exact','high','low')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  start_content_hash TEXT,
  end_content_hash TEXT
);
INSERT INTO memory_source_bindings_v2 SELECT * FROM memory_source_bindings
  WHERE bind_confidence <> 'revoked';
INSERT INTO relation_corrections(correction_id, domain,
  original_instance_id, endpoint_a, endpoint_b, original_meta,
  original_created_by, original_created_at, corrected_by, corrected_at,
  reason_code, note)
SELECT 'corr_' || lower(hex(randomblob(12))), 'source_binding',
  binding_id, memory_id, conversation_id,
  json_object('start_message_id', start_message_id,
              'end_message_id', end_message_id,
              'legacy_revoked', 1),
  created_by, created_at, 'legacy_migration', datetime('now'),
  'binding_error', 'legacy 未记录纠错人（迁移保留未知）'
  FROM memory_source_bindings WHERE bind_confidence='revoked';
DROP TABLE memory_source_bindings;
ALTER TABLE memory_source_bindings_v2 RENAME TO memory_source_bindings;
CREATE INDEX idx_msb_memory ON memory_source_bindings(memory_id, bind_confidence);
CREATE INDEX idx_msb_conv ON memory_source_bindings(conversation_id);

-- ⑤ 删除申请表：Memory-only 重定义
CREATE TABLE deletion_requests_v2(
  request_id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL,
  human_reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','approved','rejected','withdrawn','superseded')),
  rejection_reason TEXT,
  submitted_by TEXT NOT NULL,
  submitted_local_date TEXT NOT NULL,
  decided_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_deletion_resource_v2 ON deletion_requests_v2(memory_id);
CREATE INDEX idx_deletion_status_v2
  ON deletion_requests_v2(status, submitted_local_date);
-- CB-004（2026-10-02 审计 P1）：升级不得无条件清空现存申请历史。
-- memory/delete 申请与现行表语义一致，逐列映射保留（人类理由、拒绝
-- 理由=旧 ai_reason、配额依据 local_date、created_at=submitted_at）；
-- 退役产品记录（letter、memory/archive）分离保留到 legacy 专表——
-- 历史可考、不进现行表、不计现行配额。
INSERT INTO deletion_requests_v2(request_id, memory_id, human_reason,
  status, rejection_reason, submitted_by, submitted_local_date,
  decided_at, created_at)
SELECT id, resource_id, human_reason, status,
  CASE WHEN status='rejected' THEN NULLIF(ai_reason, '') ELSE NULL END,
  submitted_by, local_date, decided_at, submitted_at
FROM deletion_requests
WHERE resource_kind='memory' AND action='delete';
CREATE TABLE deletion_requests_legacy AS
SELECT * FROM deletion_requests
WHERE NOT (resource_kind='memory' AND action='delete');
DROP TABLE deletion_requests;
ALTER TABLE deletion_requests_v2 RENAME TO deletion_requests;

-- ⑥ visibility：CHECK 只含 active/hidden——无部署库（原 0925 空根
--    已删除），fresh 建库即净，无兼容包袱
"""),
]


WORKSPACE_MIGRATIONS: list[tuple[int, str]] = [
    (4, """
DROP TABLE IF EXISTS v2_review_items;
DROP TABLE IF EXISTS v2_proposal_versions;
"""),
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



CREATE TABLE recall_query_revisions(
  session_id TEXT NOT NULL REFERENCES recall_sessions(session_id),
  revision INTEGER NOT NULL,
  request_ref TEXT,
  query_plan TEXT NOT NULL,
  change_reason TEXT,
  burst_no INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  PRIMARY KEY(session_id, revision)
);

CREATE TABLE recall_candidates(
  session_id TEXT NOT NULL REFERENCES recall_sessions(session_id),
  candidate_ref TEXT NOT NULL,
  resource_ref TEXT NOT NULL,
  channel TEXT NOT NULL,
  representation TEXT NOT NULL,
  content_version TEXT,
  representation_version TEXT,
  state TEXT NOT NULL CHECK(state IN
    ('seen','rejected','accepted','deferred')),
  score_ref TEXT,
  reject_target TEXT,
  first_seen_revision INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, candidate_ref)
);
CREATE INDEX idx_recall_candidates_resource ON recall_candidates(session_id, resource_ref);

CREATE TABLE recall_attempts(
  session_id TEXT NOT NULL REFERENCES recall_sessions(session_id),
  operation_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  burst_no INTEGER NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('reserved','running','completed','failed','cancelled')),
  budget_snapshot TEXT,
  provider_versions TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, operation_id)
);

CREATE TABLE recall_receipts(
  session_id TEXT NOT NULL REFERENCES recall_sessions(session_id),
  receipt_id TEXT PRIMARY KEY,
  resource_ref TEXT NOT NULL,
  content_version TEXT,
  representation_version TEXT,
  permission_version TEXT,
  valid_at TEXT NOT NULL,
  expires_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_recall_receipts_resource
  ON recall_receipts(session_id, resource_ref);

CREATE TABLE recall_operation_keys(
  principal_id TEXT NOT NULL,
  operation_key TEXT NOT NULL,
  result_ref TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(principal_id, operation_key)
);
"""),
    (6, """
-- ===== 旧审批/工具人体系退役（2026-10-01，D09/D12）=====
DROP TABLE IF EXISTS worker_runs;
DROP TABLE IF EXISTS proposal_versions;
DROP TABLE IF EXISTS work_items;
DROP TABLE IF EXISTS workspace_task_leases;
"""),
]


def migrate() -> None:
    config.ensure_dirs()
    with db.formal() as conn:
        _require_identity(conn, "formal_v1")
        _apply(conn, FORMAL_MIGRATIONS)
        _stamp_identity(conn, "formal_v1")
    with db.workspace() as conn:
        _require_identity(conn, "workspace_v1")
        _apply(conn, WORKSPACE_MIGRATIONS)
        _stamp_identity(conn, "workspace_v1")


def _require_identity(conn, expected: str) -> None:
    """启动身份校验（v1.4 §10.1 / OPS-RECALL-01）。

    - 全新空库：必须显式 MARIPOSA_ALLOW_CREATE=1（测试根由 conftest 打开；
      生产首建/换根走显式允许），否则启动失败，不静默建空正式库；
    - 已有库：mariposa_db_meta.identity 不匹配时拒绝（防错误文件冒名）。
      迁移 13 之前的旧库无 meta 行，视为待补章的既有正式库，放行补章。
    """
    fresh = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
    ).fetchone() is None and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_items'"
    ).fetchone() is None
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table'"
        " AND name='mariposa_db_meta'").fetchone()
    if has_meta:
        row = conn.execute(
            "SELECT value FROM mariposa_db_meta WHERE key='identity'").fetchone()
        if row is not None:
            if row["value"] != expected:
                raise RuntimeError(
                    f"数据库身份不匹配：期望 {expected}，实际 {row['value']}"
                    "（可能指向了错误的库文件；拒绝启动）。")
            return
    if fresh and not config.ALLOW_DB_CREATE:
        raise RuntimeError(
            "目标路径下没有既有数据库且未显式 MARIPOSA_ALLOW_CREATE=1："
            "拒绝静默创建新的正式/工作区库（OPS-RECALL-01）。生产首次"
            "建库或迁移到新数据根时请显式设置该变量并在完成后关闭。")


def _stamp_identity(conn, identity: str) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mariposa_db_meta("
        " key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT OR IGNORE INTO mariposa_db_meta(key, value)"
        " VALUES('identity', ?)", (identity,))


RUNTIME_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE recall_sessions(
  session_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  conversation_scope TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN
    ('ACTIVE','AMBIGUOUS','CONFLICT','DEGRADED','BUDGET_EXHAUSTED',
     'RESOLVED','CANCELLED','EXPIRED','STALE_RETRY_REQUIRED')),
  current_revision INTEGER NOT NULL DEFAULT 0,
  current_burst INTEGER NOT NULL DEFAULT 1,
  rounds_used INTEGER NOT NULL DEFAULT 0,
  bursts_used INTEGER NOT NULL DEFAULT 1,
  policy_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX idx_recall_sessions_expiry ON recall_sessions(expires_at, status);

CREATE TABLE recall_query_revisions(
  session_id TEXT NOT NULL REFERENCES recall_sessions(session_id),
  revision INTEGER NOT NULL,
  request_ref TEXT,
  query_plan TEXT NOT NULL,
  change_reason TEXT,
  burst_no INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  PRIMARY KEY(session_id, revision)
);

CREATE TABLE recall_candidates(
  session_id TEXT NOT NULL REFERENCES recall_sessions(session_id),
  candidate_ref TEXT NOT NULL,
  resource_ref TEXT NOT NULL,
  channel TEXT NOT NULL,
  representation TEXT NOT NULL,
  content_version TEXT,
  representation_version TEXT,
  state TEXT NOT NULL CHECK(state IN
    ('seen','rejected','accepted','deferred')),
  score_ref TEXT,
  reject_target TEXT,
  first_seen_revision INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, candidate_ref)
);
CREATE INDEX idx_recall_candidates_resource ON recall_candidates(session_id, resource_ref);

CREATE TABLE recall_attempts(
  session_id TEXT NOT NULL REFERENCES recall_sessions(session_id),
  operation_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  burst_no INTEGER NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('reserved','running','completed','failed','cancelled')),
  budget_snapshot TEXT,
  provider_versions TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, operation_id)
);

CREATE TABLE recall_receipts(
  session_id TEXT NOT NULL REFERENCES recall_sessions(session_id),
  receipt_id TEXT PRIMARY KEY,
  resource_ref TEXT NOT NULL,
  content_version TEXT,
  representation_version TEXT,
  permission_version TEXT,
  valid_at TEXT NOT NULL,
  expires_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_recall_receipts_resource
  ON recall_receipts(session_id, resource_ref);

CREATE TABLE recall_operation_keys(
  principal_id TEXT NOT NULL,
  operation_key TEXT NOT NULL,
  result_ref TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(principal_id, operation_key)
);
-- status/payload_hash/updated_at 列由 migration 3 统一补出
--（fresh 库也走 ALTER，避免双处定义漂移）
"""),
    (2, """
-- Jev 派生缓存：只保存指纹、标量判断与版本元数据，不保存记忆正文。
-- 与 Recall Session TTL 解耦；缓存可删除重建，绝不是正式记忆真源。
CREATE TABLE jev_rerank_cache(
  cache_key TEXT PRIMARY KEY,
  query_fingerprint TEXT NOT NULL,
  candidate_fingerprint TEXT NOT NULL,
  candidate_ref TEXT NOT NULL,
  candidate_version TEXT,
  representation_version TEXT,
  projection_version TEXT,
  requested_model TEXT NOT NULL,
  resolved_model TEXT,
  prompt_version TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  relevance_signal REAL NOT NULL,
  evaluation_status TEXT NOT NULL,
  provider_receipt_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_used_at TEXT NOT NULL
);
CREATE INDEX idx_jev_rerank_candidate
  ON jev_rerank_cache(candidate_ref, candidate_version);
CREATE INDEX idx_jev_rerank_last_used
  ON jev_rerank_cache(last_used_at);

-- 静态 feature 缓存底座：只供以后明确批准的 derived feature 使用。
-- 不自动生成“心情/分类”等业务字段，更不得覆盖正式 memory 数据。
CREATE TABLE jev_feature_cache(
  feature_key TEXT PRIMARY KEY,
  resource_ref TEXT NOT NULL,
  content_version TEXT,
  projection_version TEXT,
  feature_name TEXT NOT NULL,
  feature_schema_version TEXT NOT NULL,
  requested_model TEXT NOT NULL,
  resolved_model TEXT,
  feature_value TEXT NOT NULL,
  provider_receipt_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_used_at TEXT NOT NULL
);
CREATE INDEX idx_jev_feature_resource
  ON jev_feature_cache(resource_ref, content_version, feature_name);
"""),
    (3, """
-- ===== 审计 F26/F07：runtime 操作幂等原子认领 =====
-- 旧库补列（新库已由 migration 1 直接建出）：status 三态
-- running/completed/failed；payload_hash 区分同 key 异请求。
-- 既有行 result_ref 有值视为 completed（默认值兼容），NULL 行
-- 会被当作可重新认领的失败记录，安全。
ALTER TABLE recall_operation_keys ADD COLUMN status TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE recall_operation_keys ADD COLUMN payload_hash TEXT;
ALTER TABLE recall_operation_keys ADD COLUMN updated_at TEXT;
"""),
    (4, """
-- ===== Recall 幂等与崩溃恢复（commit-at-end，2026-09-29）=====
-- 预算事实源改为"成功提交的 round 记录"；session 行上的 rounds_used
-- 不再是权威（读时由本表派生覆盖）。旧数据按 rounds_used 数量回填
-- round 行（burst 窗口按默认 RECALL_BURST_ROUNDS=3 推算；若部署曾改
-- 该配置，仅影响旧 session 的 burst 归属展示，不影响总量语义）。
CREATE TABLE recall_rounds(
  session_id TEXT NOT NULL REFERENCES recall_sessions(session_id),
  round_no INTEGER NOT NULL,
  burst_no INTEGER NOT NULL DEFAULT 1,
  operation_key TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(session_id, round_no)
);
CREATE INDEX idx_recall_rounds_op ON recall_rounds(operation_key);
INSERT INTO recall_rounds(session_id, round_no, burst_no, operation_key, created_at)
WITH RECURSIVE seq(sid, n) AS (
  SELECT session_id, 1 FROM recall_sessions WHERE rounds_used > 0
  UNION ALL
  SELECT sid, n + 1 FROM seq
  WHERE n < (SELECT rounds_used FROM recall_sessions WHERE session_id = sid)
)
SELECT sid, n, ((n - 1) / 3) + 1, NULL,
       (SELECT updated_at FROM recall_sessions WHERE session_id = sid)
FROM seq;
-- commit-at-end 模型不存在中间态：清掉上一阶段的 running/failed 残留
--（running 行的副作用状态不可知，删除后同 key 重试将重新完整计算）
DELETE FROM recall_operation_keys WHERE status <> 'completed';
"""),
    (5, """
-- ===== 审计 N08：无法验证身份/结果的旧 operation 行清理 =====
-- commit-at-end 下重试会重新完整计算，删除这些行只是让旧缓存响应
-- 失效，不产生副作用风险（重放本就必须通过当前状态重校验）。
-- payload_hash 为 NULL 的旧 completed 行无法验证同 key 异请求；
-- result_ref 为空的行无法重放。
DELETE FROM recall_operation_keys
 WHERE payload_hash IS NULL OR result_ref IS NULL OR result_ref = '';
"""),
    (6, """
-- ===== WP04/S13：Round1 成功回执 + round 逻辑类别 =====
-- 回执绑定 session/revision/plan hash/scope hash/policy/覆盖/judge
-- 统计——recall_attempts 的 completed 不再被当作 Jev 正常证据。
CREATE TABLE recall_round1_receipts(
  session_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  plan_hash TEXT NOT NULL,
  scope_hash TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  round_kind TEXT NOT NULL DEFAULT 'memory',
  methods TEXT NOT NULL DEFAULT '{}',
  coverage TEXT NOT NULL DEFAULT '{}',
  candidate_set_hash TEXT NOT NULL,
  judged_count INTEGER NOT NULL DEFAULT 0,
  unavailable_count INTEGER NOT NULL DEFAULT 0,
  unjudged_count INTEGER NOT NULL DEFAULT 0,
  delivery_action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(session_id, revision)
);
-- S17：区分 logical kind（memory/words/raw）与记录序号
ALTER TABLE recall_rounds ADD COLUMN kind TEXT NOT NULL DEFAULT 'memory';
"""),
    (7, """
-- ===== 三轮复审#2：raw round2 分页游标（服务端签发，单活跃） =====
-- 翻页是同一 logical raw round 的延续：不消耗新轮、不触发
-- no_prior_raw_round 门禁。token 服务端生成，绑定当前
-- session/revision/burst；翻尽、refine 前进 revision/burst 或
-- 重签发即失效（单活跃游标，旧 token 不可重放）。
CREATE TABLE recall_raw_continuations(
  session_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  burst_no INTEGER NOT NULL,
  token TEXT NOT NULL,
  next_offset INTEGER NOT NULL,
  issued_at TEXT NOT NULL,
  PRIMARY KEY(session_id, revision, burst_no)
);
"""),
    (8, """
-- ===== 全量审计 P1-01（2026-10-01）：Round1 完成事实显式化 =====
-- 统计回执不再隐含"本 revision 真正完整完成"：completed 只在
-- mark_round1_complete=True 的同一最终事务里置 1。旧数据默认 0
--（不可证明则不可升级——旧 session 的 round2 升级被拒，不伪造）。
ALTER TABLE recall_round1_receipts
  ADD COLUMN completed INTEGER NOT NULL DEFAULT 0;
"""),
    (9, """
-- ===== 复审（2026-10-01）：raw-per-burst 唯一不变量 =====
-- 并发 Round2 的最后防线（进程锁 + 事务内复查之外的 DB 层不变量）：
-- 每 (session, burst) 至多一条 kind='raw' 轮。若存量数据违反（历史
-- 并发脏数据）建索引失败即迁移失败——fail fast，不静默取舍。
CREATE UNIQUE INDEX idx_recall_rounds_raw_per_burst
  ON recall_rounds(session_id, burst_no) WHERE kind='raw';
"""),
]


def migrate_runtime() -> None:
    """Recall Session 运行库（runtime/recall/recall.sqlite3）。

    与正式库身份校验同一原则：运行库可随隔离根新建（测试根必然新建），
    但生产数据根未显式 ALLOW_CREATE 时不在此处新建文件。
    """
    config.RECALL_DB.parent.mkdir(parents=True, exist_ok=True)
    with db.recall_runtime() as conn:
        if not config.ALLOW_DB_CREATE and not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='recall_sessions'").fetchone() and conn.execute(
                "SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            raise RuntimeError("recall 运行库文件已存在但结构不可识别；拒绝复用。")
        _apply(conn, RUNTIME_MIGRATIONS)


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
