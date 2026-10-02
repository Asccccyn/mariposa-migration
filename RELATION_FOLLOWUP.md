# Relation Followup（2026-10-01，代码核证版）

只记录，不修改。每条事实附代码/数据依据；不含任何已实施的新架构。
供江乔生、林石见对齐 Relation 语义用。

## 0. 总形态：当前是四套独立的关系表达（+一处准关系）

| # | 表 | 连接什么 | 类型词表 | 版本粒度 | 代码 |
|---|---|---|---|---|---|
| 1 | `memory_relations` | 桶↔桶 | continuation_of / related_to / contradicts / custom（服务层白名单；**表无 CHECK**） | 无版本（桶正文修订后关系不变） | memory/relations.py |
| 2 | `i_revision_memory_relations` | I 条目**修订**↔桶 | changed_because_of / clarified_by / informed_by / related（表 CHECK 闭集） | 修订级（绑 item_id+revision） | identity_i/service.py |
| 3 | `memory_source_bindings` | 桶↔原文**区间** | 无类型；bind_confidence ∈ exact/high/low/revoked | 目标不可变（source 消息版本化，message id 稳定） | source/binding.py |
| 4 | `plan_memory_links` | plan↔桶 | 无类型无语义（纯成员链接，驱动 P1-08 阶段接线） | 无版本 | plans/service.py |
| 5(准) | `our_words.source_ref` | 话语↔原文/绑定 | 前缀即类型（source_msg: 现行；raw_msg:/raw_binding: 已随 D13 退役=恒 invalid） | 绑 word 指纹（speaker/text/kind/source/version 变则失效） | retrieval/words.py |

## 1. 逐问事实

**Q1 现有哪些 resource type？**
没有统一的 resource type 词汇表。各表用各自 FK + 代码层的 ref 前缀
（memory:<id> / our_word:<id> / source_msg:<id> / plan 裸 id / I 用
item_id+revision 复合）。前缀体系散见于 evidence、recall candidates、
words、source，**relation 层本身不消费前缀**——只认本表 FK。

**Q2 现有哪些 relation type？**
四套词表互不重叠（见上表）。注意 `related_to`（桶间）与 `related`
（I→桶）是两个不同词表里的近义词。`continuation_of` 是唯一被
trace 消费的类型。

**Q3 Memory↔Memory 怎么表达？**
memory_relations 单行 = 一条有向边：from→to + type + 可选
custom_label/reverse_label + confidence(默认 'human'，服务层写死，
表有列但未开放) + active + version。PK=(from,to,type) 同对同类型
只有一行（INSERT OR IGNORE）。detach=软删（active=0, version+1，
留行留历史）。

**Q4 Memory↔I 是普通 relation 还是另一套？**
另一套。I 条目每次 revise/restore 产生新修订，关系写在
i_revision_memory_relations 且**绑定到该修订**（不是条目当前态）；
同一 I 条目不同修订可以指向不同 memory 集合。另有 I 内部修订链
（based_on_revision / informed_by_revision / restored_from_revision，
存 i_item_revisions 行内）——那是修订自我引用，不进关系表。
即 I 侧实际是两层：修订链（行内）+ 修订→memory（关系表）。

**Q5 Memory↔Source 是不是又一套？**
是。memory_source_bindings 绑到（conversation, start/end message,
可选 char offset）——**区间级**，非整资源。语义是证据溯源（这段
原文支持这个桶），不是桶间那种语义关系。revoke=状态位改
'revoked'（留行）。bind_confidence 是四套里唯一真正在用的置信度。

**Q6 Plan binding 算不算 Relation？**
现状：不算语义 relation，是成员链接（无类型/方向/历史），但承载
真实运行语义——P1-08 后 plan 分类的桶的阶段由所链 plan 的状态决定
（任一 open→WIDE / 全终态→CORE）。它在 phase 接线里的地位与
relation 不同源。

**Q7 revision 和 resource identity 怎么区分？**
三种粒度并存（上表第 4 列）：桶间关系不感知正文版本；I 关系锚定
修订号；Source 目标天然不可变。没有跨三者的统一"资源身份"抽象。

**Q8 删除 Letter/Diary/Quote 等留下哪些旧 relation type？**
**零。**生产库取证（2026-10-01 只读）：memory_relations **0 行**、
plan_memory_links 0 行；i_revision_memory_relations 与
memory_source_bindings 两表在生产库**尚不存在**（生产库为 0925 旧
schema，部署时随迁移创建）。即：**四套关系在生产全是白纸**，
语义定死不受任何存量数据约束。schema CHECK 里也没有指向已删模块
的类型枚举。

**Q9 单向/有向/反向语义？**
全部单向存储。桶间关系反向靠显式查询（list direction=in 时标注
reversed=true）；custom 类型可声明 reverse_label（反向读名，唯一
的"反向语义"载体，但仍是单行存储）；related_ids（检索 related_of
途径）双向取邻居不区分方向。I/Source/Plan 无反向查询语义。

**Q10 删除关系和删除资源现在怎么处理？**
- 删关系：detach 软删留历史；binding revoke 留行；plan unlink
  随 hold 侧写（无独立 revoke 能力——plan_memory_links 无 detach
  入口，只能随 plan/memory 删除消失）。
- 删资源（deletion/service._execute）：**不对称**——有 I 修订关系
  或 Source 绑定引用的桶**禁止物理删除**（DELETE_BLOCKED_BY_
  REFERENCES，建议 archive）；而 memory_relations 与 plan_memory_
  links **不阻止**，桶被物理删除时随之 DELETE（_execute 清单
  224-231 行）。即：跨域历史引用（I/Source）> 删除权 > 桶间关系。
- archive：全部关系原样保留（只隐藏投影）。

**Q11 OST 图谱能否直接建在现有 Relation 上？（事实层陈述）**
现有四表能直接给出：桶-桶边（4 类型+custom 双标签）、I修订-桶边
（4 类型）、桶-原文区间边（证据+置信度）、plan-桶成员边。
事实上的缺口：①无统一资源命名/类型词汇（各表 FK 各认各的）；
②`related_to`/`related` 近义不重叠；③除 created_at 外无边时间
语义；④confidence 只有 Source 侧真在用；⑤桶间关系不感知正文版本
（图上的节点是"桶"而非"桶的某版本"）。哪些算缺口、哪些算合理
分层——留对齐裁定。

**Q12 还有什么隐藏的"关系"？**
- recollections 的 supersedes 链（桶内层，非跨资源）；
- memory_relations.version 字段（软删计数，非修订语义）；
- deletion_requests 本身（资源→删除决议，审计链）。

## 2. 供对齐的决策清单（只列问题，不给方案）

1. 四套表达是否维持各司其职，还是需要统一的资源身份层？
2. `related_to`（桶间）与 `related`（I 侧）是否要统一词表？
3. 桶间关系要不要感知正文版本（现状：不感知）？
4. plan 链接要不要 detach 能力与历史？
5. 删除不对称（I/Source 引用阻止删除、桶间关系随删）是否符合
   预期？
6. memory_relations 的 confidence 字段（现在写死 human）留/用/删？
7. OST 图谱的节点粒度（桶 vs 修订）与边词汇是否以现状为基？
8. 上轮审计 F-47（navigate 未复用 AllowedScope）是否并入本轮。

## 3. 本轮（退役轮）对 Relation 的全部动作

零修改。回归：test_retired_capabilities_negative.py::test_relation_
untouched（link+list+表可写读）+ 既有 relations 测试全绿。
