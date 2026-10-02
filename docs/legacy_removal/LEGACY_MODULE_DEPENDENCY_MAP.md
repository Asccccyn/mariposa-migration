# Legacy Module Dependency Map（2026-10-01，Phase 1）

Baseline: db6fa36 + D01(letters) @ 610317c。生产库取证（只读 COUNT）：
**全部退役模块表 row_count=0**（home/home_versions/diary_*/self_*/moments/
moment_*/stickers/quotes/quote_versions/reminders/raw_conversations/
raw_messages/memory_raw_refs/memory_meanings/letters/letter_versions/
media_objects/provisional_sources 均 0 行；work_items/proposal_versions
表不存在于生产库）→ §7 全部可 DROP，无 LEGACY_TABLE_DATA_PRESENT。

| D | module | files | public capabilities | tables | 外部依赖（module 之外） | decision |
|---|---|---|---|---|---|---|
| 01 | Letter | letters/（已删）+deletion/（保留） | letter.* ×4（已摘） | letters, letter_versions（migration 24 已 DROP） | migration.py：letter→out_of_scope（已改） | DONE 610317c |
| 02 | Home/Self/Diary | content/service.py, content/__init__.py | home.get/update, self.write/list/review/revise/retire, diary.write/list/search/hide（11 caps） | home, home_versions, self_entries, self_versions, diary_entries, diary_versions | migration.py _TYPE_TARGET self/diary→self_entries/diary_entries（迁移目标，需改 out_of_scope）；test_gate_cases2 断言 calendar.PROVIDERS 无 letter/self/diary | DELETE（表 0 行） |
| 03 | Calendar | calendar/service.py | calendar.day/range/month/undated（4） | 无专表（聚合层） | test_gate_cases2 PROVIDERS 断言；web UI | DELETE |
| 04 | Reminder | reminders/service.py + maintenance.reminders_fire_due | reminder.create/list/cancel（3）+ maintenance.reminders.fire_due | reminders | maintenance 其余逻辑独立保留 | DELETE（fire_due 函数一并摘） |
| 05 | Moments | moments/service.py | moments.post/list/comment/react（4） | moments, moment_versions, moment_comments, moment_reactions | media（保留，引用摘除） | DELETE |
| 06 | Sticker | registry 内 handlers（无独立目录） | sticker.list/add/search（3） | stickers | media（保留）；表情包职责归 estómago | DELETE |
| 07 | Quotes | quotes/service.py | memory.quotes.keep/list/search/withdraw/get/by_memory（6） | quotes, quote_versions | our_words/words 专项为现行体系（不迁移，0 行） | DELETE |
| 08 | semantic_review | quotes/semantic_review.py | workspace.quotes.review.run/reviews.list（2） | 无专表（复用 quote 状态） | 仅 registry 引用 | DELETE（随 quotes/） |
| 09 | Candidate/Review/Proposal | v1_compat.py 内 handlers/aliases | workspace.candidates.create, memory.candidates.confirm/reject/rewrite, deletion.restore 兼容别名, review/proposal/forgetting aliases | work_items, proposal_versions, proposal_envelopes, proposal_resolutions, review_delegations, rejection_suppression, forgetting_due_queue, memory_summary_versions, memory_retention | v1_compat 其余现行 aliases 保留；deletion_get/deletion_restore 指向现行 deletion 模块（保留） | DELETE（逐 handler；部分表已在早前迁移 DROP） |
| 10 | Raw binding review 残链 | raw/binding.py low-conf 分支 | （随 D13 一并） | work_items/proposal_versions 写入 | 无独立依赖 | DELETE（随 D13） |
| 11 | Meaning | memory/listing.py meanings_* | memory.meanings.append/replace/list（3） | memory_meanings | field_projection 不含 meaning（禁检）✓ | DELETE（0 行，不迁移） |
| 12 | Workspace Tasks/Lease | workspace/tasks.py | workspace.tasks.list/claim/release（3，v1_compat thin） | work_items | 调用方仅 v1_compat candidate/review（D09 删）与 raw binding review（D10 删） | DELETE（无现行依赖） |
| 13 | Legacy Raw | raw/（service.py, binding.py, recall.py, __init__.py） | raw.import/messages.list/search/conversations.list/provisional.report/binding.bind/binding.revoke/binding.refs（8） | raw_conversations, raw_messages, memory_raw_refs, provisional_sources | ①recall/service.py:26 import raw.recall=**死 import**（摘）；②bootstrap 一条 note 文案（改指 Source）；③words._source_ref_valid 查 raw_messages/memory_raw_refs（旧前缀 raw_msg:/raw_binding:，表 0 行→退役旧前缀分支，新 source_msg: 不受影响）；④deletion 模块不引用 ✓ | 情形 A：DELETE（0 行、无主链依赖；Round2 走新 Source 层） |

Relation（冻结区）：memory_relations/i_revision_memory_relations 表与
relation.trace 不动；退役模块不持有 relation target_type（生产 relation
数据仅 memory/I/Source 侧——见 RELATION_FOLLOWUP.md）。
Media：保留；删除 moments/sticker 引用后剩余 callers 见
MEDIA_DEPENDENCY_REPORT（附录）。
