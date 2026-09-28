# Mariposa Source Layer 实施报告

日期：2026-09-27　｜　实施：程知行（ZCode）　｜　验收对象：《原文层实施规格 v1》（十九条）

---

## 1. 本次新增架构

```
Claude Export（.json / .zip）
        │  PUT /api/source/upload（流式暂存）或 宿主本地路径
        ▼
Source Importer（source/importer.py）
        │  流式 SHA-256 → 幂等检测 → provider 识别（adapters/registry）
        ▼
Raw Archive（runtime/source/raw/claude/<batch_id>/，只读母本）
        │  json_stream 增量解析（不整文件驻内存）
        ▼
Claude Adapter（source/adapters/claude.py）
        │  UUID 身份 / sender 精确映射 / 正文与证据分离 / 业务时区自然日
        ▼
Normalized Source Store（source_* 表 + source_fts 检索投影）
        │
        ├── Source Query（source/query.py）：source.search / message.get /
        │   range.open / conversation.get —— 独立能力，非普通 Recall
        ▼
Semantic Source Binding（memory_source_bindings）—— Memory 引用原文，不复制原文
```

分层边界：普通 Recall（memory.recall / memory.search / words 通道）完全不扫描
Source Store；source.* 是独立专项检索，结果标 `source=source_layer` 并声明
「不属于记忆召回」。

## 2. 新增 / 修改文件清单

**新增（12 个）**

| 文件 | 职责 |
|---|---|
| backend/mariposa/source/__init__.py | Source Layer 包说明 |
| backend/mariposa/source/json_stream.py | 顶层 JSON 数组增量解析（stdlib raw_decode，1MB 块） |
| backend/mariposa/source/archive.py | Raw Archive 只读落盘 / manifest / 校验 |
| backend/mariposa/source/importer.py | 导入管线（13 步全流程） |
| backend/mariposa/source/query.py | 专项查询服务 |
| backend/mariposa/source/binding.py | memory↔source 语义绑定 + 检索投影重建 |
| backend/mariposa/source/adapters/__init__.py | adapter 注册表（detect_provider） |
| backend/mariposa/source/adapters/base.py | 抽象基类 + SPEAKER_MAP |
| backend/mariposa/source/adapters/claude.py | Claude Export 标准化 |
| tests/unit/test_source_layer.py | 单元/管线测试（54 项） |
| tests/integration/test_source_api.py | HTTP/MCP 集成测试（7 项） |
| tests/fixtures/claude_export/*.json（5 个） | 合成 fixture（无真实隐私内容） |

**修改（6 个，均为最小边界修改）**

| 文件 | 修改原因 | 旧行为 | 新行为 | 影响既有 API | 测试覆盖 |
|---|---|---|---|---|---|
| backend/mariposa/schema.py | 追加迁移 14 | 迁移链止于 13 | 新增 source_* 六表 + FTS（见 §3） | 无 | test_source_layer 全部 |
| backend/mariposa/config.py | 数据根目录常量 | 无 source 目录 | SOURCE_RAW_DIR / SOURCE_INCOMING_DIR / SOURCE_PROJECTION_VERSION + ensure_dirs | 无 | 冒烟+集成 |
| backend/mariposa/capabilities/registry.py | 注册 source.* | 无 | 12 个新 capability（owners 专用） | 不改任何既有能力 | test_source_api |
| backend/mariposa/app.py | 上传字节端点 | 无 | PUT /api/source/upload（流式写盘） | 不改既有路由 | test_source_api |
| backend/mariposa/web/index.html | Viewer | 6 个页签 | 新增「原文」页签 | 不改既有页面 | 浏览器冒烟 |
| tests/conftest.py | 测试隔离 | FORMAL_TABLES 无 source 表 | 追加 6 表（子表在前） | — | 全部测试 |

## 3. 数据库 Migration（FORMAL_MIGRATIONS #14）

- `source_import_batches`：批次（provider+sha256 UNIQUE；status
  running/completed/failed；error/stats 留痕；UNIQUE 保证幂等检测）
- `source_conversations`：provider_conversation_id UNIQUE；title/时间/条数聚合
- `source_messages`：
  - 身份：provider + provider_message_id **UNIQUE**（UUID 主身份；缺 UUID 用
    批次锚定合成 ID，id_synthetic=1，绝不用「文本+时间」）
  - parent_provider_message_id 保留
  - raw_sender 原值 / normalized_sender ∈ {human,assistant,system,tool,unknown}
    CHECK / speaker ∈ {qiaosheng,jiaming} 可空 CHECK
  - created_at 原始字符串 + occurred_date 业务时区自然日
  - text 仅 human/assistant 正文；content_json 证据（base64>512 截断留标记，
    完整母本在 Raw Archive）；attachments 引用
  - has_thinking / has_tool_content 标志
  - 索引：(conversation_id,sequence)、occurred_date、normalized_sender、created_at
- `source_search_docs` + `source_fts`（FTS5）：可重建检索投影，**仅收
  human/assistant 的 text**；`binding.reindex_search_docs()` 可全量重建
- `memory_source_bindings`：binding_id PK；memory_id → conversation + 连续
  message 区间 [start,end] + 可空 char offset + confidence（exact/high/low/
  revoked）；**一 Memory 多 range 天然支持（多行）**；撤销=revoked 留历史

## 4. API / MCP 接口（与 HTTP `/api/capability/*`、MCP `/mcp` 同 Registry）

| capability | 类型 | 说明 |
|---|---|---|
| source.import | write, idempotent | {path, filename?}：宿主路径或上传返回路径 |
| source.import.status / .batches | read | 批次状态（含失败 error）/ 列表 |
| source.search | read | {query?, senders?, provider?, conversation_id?, date_from/to?, limit, offset} |
| source.message.get | read | {message_id \| provider_message_id, context, include_content} |
| source.range.open | read | {conversation_id, start/end_message_id, char_offsets, include_content} |
| source.conversation.get | read | {conversation_id, after/before/around_seq, limit} 分页 |
| source.conversations.list | read | 会话列表（含首末消息时间/条数） |
| source.binding.bind / .list / .revoke | write/read/write | memory↔range 绑定 |
| source.memory.open | read | 按 memory 动态打开绑定原文区间 |

MCP 传输名自动映射（mariposa_source_search 等），worker 无任何 source 权限
（HTTP 403 / MCP tools/list 不出现）。

## 5. Raw Archive 位置

`MARIPOSA_ROOT/runtime/source/raw/<provider>/<batch_id>/`

- 母本文件（conversations.json 或导出 zip 原名）+ manifest.json（sha256/
  bytes/来源/时间）+ metadata.json（导入统计与校验结果，完成后写入）
- 原文件字节级复制，落盘 chmod 0444；重试批次不覆盖旧母本（测试验证 mtime
  不变）；不清洗、不因重解析丢弃
- `runtime/` 整体在 .gitignore（`git check-ignore runtime/source/raw` 验证）
- 位于宿主数据根（MARIPOSA_ROOT），不在任何容器层；Mariposa 当前以宿主
  uvicorn 运行，若未来容器化，该目录必须挂载宿主卷

## 6. Adapter 规则（Claude）

改编自查看器 v4.9 的 detectFormat/getMessages/extractMessageText，转为数据保存语义：

- **detect**：元素为 dict 且 chat_messages 为 list → claude（zip 内定位
  conversations.json 成员）
- **消息身份**：uuid 为主身份；缺失时 `missing-msg-<sha256(batch:conv:seq)>`
  确定性合成（同批次重导幂等），计数进 stats
- **sender**：raw_sender 保存原值；normalized 只做**精确**映射
  human/assistant/system/tool（strip+lower 后整词匹配），其余一律 unknown。
  查看器「未知降级 assistant」的 UI 规则**未进入**本层
- **正文**：只从 content 数组 type=text 的 block（+顶层 text 回退）提取，且
  仅 human/assistant；多 text block 按 JS 语义直接拼接
- **分离**：thinking→has_thinking；tool_use/tool_result→has_tool_content；
  system/unknown 的 text 置空（证据全在 content_json）；附件取
  document/image/attachment 引用（不带字节）
- **扩展**：接入新 provider 只需实现 base 协议并在 adapters/__init__ 注册；
  DeepSeek 按用户指示本轮不做

## 7. Speaker Mapping

| normalized_sender | speaker（库内 ID） | speaker_display |
|---|---|---|
| human | qiaosheng | 江乔生 |
| assistant | jiaming | 周家明 |
| system / tool / unknown | NULL | null |

导入完整性校验含 speaker_mapping 断言（违规计 problems，不静默）。

## 8. 时间处理规则

- created_at / updated_at 保存 **provider 原始字符串**（不改写）
- occurred_date = parse(created_at，无时区按 UTC，解析失败置 NULL 不猜)
  →astimezone(ZoneInfo(config.RELATIONSHIP_TIMEZONE))→date
- 复用既有 RELATIONSHIP_TIMEZONE（默认 Asia/Shanghai）；服务器/Docker 时区
  变化不影响结果
- 边界验证：`2026-01-01T23:30Z → occurred_date 2026-01-02`（上海次日 07:30）、
  `15:59:59Z → 2026-01-01`

## 9. 检索边界（与普通 Recall 严格分层）

- source.search 默认只查 human/assistant 的 text（FTS source_fts 独立索引）
- thinking / tool_result / system 文本**默认不可检索**；显式
  senders=["system","tool","unknown"] 时走 content_json 证据匹配（LIKE），
  excerpt 从证据提取
- memory 的 search_fts / retrieval_documents / words_fts 与 raw_* 层完全不受
  导入影响（测试断言零行）
- 既有 raw.*（v1 合成导入层）与新 source.* 并存互不影响

## 10. Semantic Source Binding 设计

- 默认 message boundary：start/end_message_id（内部 ID 或 provider UUID 双路解析）
- 句内片段才用 char offset（可空；绑定时校验 0≤offset≤len(text)，打开时回显）
- 一 Memory 多 range：多行绑定，open_for_memory 逐一动态读取
- 跨会话区间 / 逆序区间 / 越界 offset / 不存在 memory → 结构化拒绝
- 撤销留历史（bind_confidence=revoked，行保留）；绑定写 audit_events

## 11. 性能设计

- json_stream：1MB 块 + raw_decode 逐元素；内存上界 = 单 conversation + 1 块，
  与文件总量无关（测试构造 >chunk 元素验证跨块解析）
- 逐 conversation 单事务写入；每会话先预取已存在 ID 集（一次查询代替逐条探测）
- SHA-256 / 归档复制均 1MB 块流式
- 上传端点 async 逐块写盘（不驻内存）
- 会话读取 sequence 游标分页（3000 条消息测试通过）
- source_fts 检索投影可由 source_messages 全量重建（reindex_search_docs）

## 12. 导入流程与完整性（§13/§14 落地）

13 步全实现；失败路径：格式识别失败也建 failed 批次（provider=unknown）留痕；
中途异常按会话事务回滚、批次标 failed（error 落库 + metadata.json），同文件
重导自动续传（已存消息跳过、只补缺失——测试验证 2 存 10 补）。

导入输出统计：会话数（新/复用/空/解析失败）、消息数（新/跳过/文件内重复/
缺 UUID）、五类 sender 计数、有 text/thinking/tool/附件数、最早最晚
created_at、verify_problems。完整性抽样：speaker 映射、正文只属双方、
UUID 无重复、FTS 覆盖=正文数、occurred_date 覆盖、parent UUID 保留数、
Raw Archive 哈希一致。

## 13. 测试列表及结果

**tests/unit/test_source_layer.py（54 项）**：sender 精确映射矩阵、unknown
绝不映射 assistant、顶层空 text/content 提取、thinking+text 分离、tool_use/
tool_result 不入正文、system 不入正文、UTC 跨日边界（双向）、naive 时间按
UTC、parent UUID、缺 UUID 合成确定性、base64 截断、同文不同 UUID、
json_stream（小数组/跨块/非数组/截断/空数组）、标准导入统计、幂等重导、
新批次同会话零重复、边界计数、跨日 occurred_date、空会话、坏 JSON 留痕、
无法识别格式、zip 导入、3000 条长会话+分页、中断恢复、Raw Archive 字节
一致+不可覆盖、正文检索命中/默认不查 thinking/工具、证据面检索、日期区间、
会话过滤、sender 过滤、UUID 精确打开+上下文、range 打开/逆序拒绝/跨会话
拒绝、会话列表、绑定打开、多 range、char offset 越界、跨会话绑定拒绝、
撤销留史、memory 索引零污染、投影重建、真实导出可选只读（默认 skip）。

**tests/integration/test_source_api.py（7 项）**：上传鉴权（401/403）、
HTTP 全流程（上传→导入→搜索→会话→消息→绑定→动态打开）、worker 禁入、
source 内容不进 memory.search、MCP tools 暴露与 worker 屏蔽、坏 JSON
结构化拒绝+批次留痕。

**浏览器冒烟（隔离实例 18799）**：登录→原文页→路径导入→会话列表→会话
查看（气泡/说话人/精确时间/thinking 徽标/证据折叠）→搜索命中→点击跳转→
XSS 渲染验证（script/img 零执行、Markdown 加粗正常、转义文本显示）。

结果：**61 passed + 1 skipped（可选真实导出）；全量回归 unit 340 /
integration 37 / acceptance 119 全绿**（合计 496 passed）。

## 14. 已知限制

1. **既有 flaky 测试（非本轮引入）**：tests/unit/test_recall_session.py
   `test_session01_service_full_loop` 间歇失败（约 1/3 概率，
   `assert nav["candidates"]` 空；改动前代码 8 次复现 3 次同样失败，已用
   git stash 对照验证）。属 recall navigate 的非确定性问题，按规格十六
   「禁止顺手修改已审计语义」本轮不动，留待单独决策。
2. source.import 的 {path} 是宿主本地路径读取（仅 owners 可用，127.0.0.1
   绑定）——单机私人服务的既有信任模型内。
3. 证据面（system/tool/unknown）关键词匹配用 LIKE，超大规模时性能一般；
   正文主路径走 FTS。
4. content_json 中 base64 载荷 >512 字节截断（母本完整在 Raw Archive）。
5. Viewer 按月/日历视图、心动曲线、词云、头像主题等查看器美化功能未做
   （规格十明确不扩大范围）；当前提供列表+搜索+会话分页+跳转。
6. 一条消息的多个 text block 按查看器语义直接拼接（无分隔符）。

## 15. 尚未实现但已预留的接口

- ChatGPT / 其他 provider adapter：base 协议 + registry 就绪，实现 detect +
  normalize 即可接入，importer/query/binding 零改动（DeepSeek 按用户指示不保留）
- 原文语义向量检索：独立 index + 独立 permission gate 的位置已留
  （source_search_docs 结构可扩展），本轮未启用
- Raw Archive 内 zip 附件（图片等 file-*.png）未提取展示，母本已完整保存
- memory.hold 界面上「从原文一键绑定」的 UI 快捷方式（API 已具备
  source.binding.bind）

## 16. 污染现有 Memory / Recall 的风险审计

未发现污染路径。具体核验：

- source 导入只写 source_* 表 + source_fts + source_search_docs +
  memory_source_bindings + audit_events/outbox + source_import_batches；
  不触碰 memories/retrieval_documents/search_fts/words_*/raw_*（测试断言零行）
- 普通 recall/raw fallback 语义未改一行
- 新 capability 全部 owners（qiaosheng/jiaming）专用；worker/linshijian 无权
- Viewer 聊天文本渲染先 HTML 转义再做有限 Markdown 转换，链接不加 href；
  实测 `<script>`/`<img onerror>` 均转义为纯文本

**交付前自审计八项**（§19 要求）：

| 检查项 | 结论 |
|---|---|
| unknown 当 assistant | 否——精确映射，测试断言 speaker NULL |
| thinking/tool 当正文 | 否——text 只来自 text block 且限双方，默认检索不可见 |
| UTC 日期错位 | 否——ZoneInfo 业务时区换算，边界双向测试 |
| 重复导入 | 否——批内 sha 幂等 + 跨批 UUID UNIQUE，双测试 |
| 一次性读整份大 JSON | 否——增量解析，跨块测试，内存上界=单会话+1MB |
| 原文进普通 Recall | 否——独立 FTS/表，memory 索引零污染断言 |
| 原始聊天写进 Git | 否——runtime/ 已忽略，fixture 全合成，git status 零导出文件 |
| 容器删除丢原文 | 否——母本在宿主 MARIPOSA_ROOT；容器化时须挂载该卷（已记录） |

—— 本报告即交付凭证；如需复验，入口：
`.venv/bin/python -m pytest tests/unit/test_source_layer.py tests/integration/test_source_api.py -q`
