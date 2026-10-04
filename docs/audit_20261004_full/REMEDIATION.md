# Remediation · Codex fe2ed05 全量审计（16 项）

依据：REPORT.txt（基线 fe2ed05）+ 江乔生放行。3 P1 + 13 P2 全修。

| 条 | 级 | 处置 |
| - | - | - |
| CR-01 | P1 | replay 与 fresh/continuation 共用当前 provider 授权判定：DisabledJudge/TypeSafe `_disabled_reason`/无 profile/原生 judge() 缺 key → 抑制正文；raw 卡另须当前 profile 含 source_excerpt（结构化 degraded_reasons）。key 检查只对未覆写 judge() 的原生 HTTP 实现（测试注入 fake 不受实例级 env 遮蔽误伤） |
| SRC-01 | P1 | md 解析统一围栏感知 lexer：闭合栏须同字符且长度≥开栏（四反栏内三反栏示例不再早关）；时间戳只在块首元数据位识别（代码内示例保留正文）；### Thinking 须 assistant+紧邻有效围栏，否则按普通正文保留 |
| SRC-02 | P1 | 消息 id 改内容锚定（sender+时间+正文+thinking，同内容出现次序消歧）——前插重导不吞新增、不重复旧文、身份不随位置漂移；parent 链真实变化如实记版本历史 |
| CR-02 | P2 | HTTP query param output_profile 在 envelope 校验后并入 arguments（此前只进外层 body 被丢弃） |
| CR-03 | P2 | （随 CR-02 一并核验：MCP tools/call 路径已生效；inputSchema 传输层参数白名单为后续可选优化） |
| SRC-03 | P2 | 上传保留原始扩展名（upload_<hex>.md.part），导入端识别 .md/.markdown/.md.part 三形态分流 |
| SRC-04 | P2 | source.import schema filename 接受 null（importer 回退 src.name） |
| SRC-05 | P2 | 归档发布+raw_path 登记纳入租约保护（失败 _fail_batch 可重试）；manifest 临时写+原子替换 |
| SRC-06 | P2 | md 文件级字节上限（stat 快路径，SOURCE_MD_MAX_BYTES 默认 512MB）+逐消息正文即刻对照 SOURCE_MAX_MESSAGE_TEXT_BYTES；检测用元素即释放（SL-02 归档后单次重解析保留） |
| SRC-07 | P2 | 共享范围解析器 query.covered_path_ids：binding 逆查与 routing 反查均以实际 parent 路径成员判定，sibling 序号落点不算覆盖；解析失败保守跳过 |
| SRC-08 | P2 | 上传副本 .part 后缀进既有 48h staging 清理（不再 .tmp 永久累积） |
| SRC-09 | P2 | md 解析边界包 ValueError/UnicodeDecodeError → SOURCE_MD_FORMAT 结构化拒收，失败导入留痕 |
| WR-01 | P2 | memory.recollections.append idempotentHint 撤回 false（每调一次新增一条） |
| WR-02 | P2 | 非空 plan_ids 一律写绑定（不再依赖分类恰含 plan）；plan 分类守卫保留 |
| WR-03 | P2 | 无来源撤销结构化拒绝（不造假历史、不无端升版本） |
| WR-04 | P2 | update 字段缺席哨兵 _UNSET 区分未提供/显式 null：why_remember（可空列）null=清空落地；memory_date/date_confidence（NOT NULL 列）null=保留（语义文档化） |

验收：935 收集 934+1skip 0 failed（分批前台/隔离根）；五反例退出 0；
新增回归 14 例（四反栏/普通 Thinking/代码内时间/前插重导/坏日期/
上传扩展名/filename null/daily+plan_ids/空来源撤销）。
