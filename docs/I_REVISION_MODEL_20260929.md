# Mariposa I 条目级版本模型

日期：2026-09-29

## 语义

I 由周家明自行形成和维护。江乔生可以查看并提出意见；只有周家明确认后，
才由周家明修改正式 I。后台不得因为 reflection、召回结果或一次对话自动
替周家明改 I。

I 现在按稳定条目（I_ITEM）维护：

- **新增是新增**：形成新的 `item_id`，revision 从 1 开始。
- **变动是变动**：既有条目修改时追加 revision，旧 revision 永久保留。
- **当前态唯一生效**：只有 `current_revision` 进入当前 I；旧 revision 不参与
  普通召回，也不默认进入 bootstrap/system context。
- **历史可见但按需**：当前条目只暴露 `has_history/history_count` 和历史存在提示；
  周家明认为当前场景需要时，显式调用 `i.item.history`。
- **回退仍然是新变化**：恢复旧 revision 不把 current 指针倒回去，而是复制旧内容
  产生新的 revision，并记录 `restored_from_revision`。
- **二次变动可引用旧自己**：如果不是完整恢复，而是重新看过旧版后形成新的表达，
  用 `informed_by_revision` 记录来源。
- **修改理由可留存**：revision 可带 `change_reason`。
- **可绑定记忆桶**：revision 可绑定 memory bucket，并注明
  `changed_because_of / clarified_by / informed_by / related`；之后按需沿 relation
  打开对应桶。这里不新增任何普通 Recall Session 路径。

## 数据状态

```text
I_ITEM i_xxx
├─ v1 create
├─ v2 revise       based_on=v1
├─ v3 revise       based_on=v2
└─ v4 restore      based_on=v3, restored_from=v1   ← CURRENT
```

存在态可以同时保存 v1..v4；生效态永远只有 v4。

## 能力接口

- `i.items.list`：当前条目，不附旧正文。
- `i.item.get`：单条当前态，不附旧正文。
- `i.item.create`：周家明新增。
- `i.item.revise`：周家明修改，可带 reason、旧 revision 启发、memory relations。
- `i.item.restore`：周家明恢复旧版，但生成新的当前 revision。
- `i.item.history`：显式读取一条 I 的完整版本链、理由和 memory relations。

旧 `i.get/i.write/i.versions.read` 保留兼容。`i.write` 只允许单条 `i_main`
兼容模式；一旦存在多个 I_ITEM，拒绝整篇覆盖，防止压平条目历史。

## 与召回的边界

本变更**不修改**普通记忆召回、找话专项、原文补查、Shared Recall Session、
否定候选状态机或 Jev 精排链路。I 历史没有写入 retrieval documents/FTS/vector
索引。历史 I 只有显式 `i.item.history` 可以打开。

## 旧数据迁移

既有正式 `i_documents/i_versions` 会原样迁为一个 `i_main` 条目，所有旧版本顺序
保留。迁移不会把旧 Self/Home 自动映射成 I。
