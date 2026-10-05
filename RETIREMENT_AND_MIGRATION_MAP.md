# Retirement & Migration Map（v2.0 收口轮）

## 2026-10-05 追加：Web React 版（apps/web）退役

| 项 | 处置 |
|---|---|
| apps/web 整目录（React 源码/dist/e2e/playwright/换窗隔离脚本） | 整体删除（她裁定"没用的直接退役"） |
| 退役理由 | ①认证停留在旧"选身份+粘贴 token"模式，未跟上 2026-10-05 OAuth 密码登录；②招牌功能（遗忘闭环、日历）属已退役业务语义；③从未接线生产，生产 `/` 一直是 backend/mariposa/web/index.html |
| 现行前端 | backend/mariposa/web/index.html（唯一，配套 OAuth password grant；token 响应增补 principal_id 供登录后显示真实身份） |
| 无 schema/数据影响 | 无表、无迁移、无合同变更；docs/ 内历史报告按惯例不改写 |

## 被删旧合同/字段/分支

| 项 | 处置 |
|---|---|
| memory.relations.detach / source.binding.revoke / memory.deletion.restore | Registry/schema/handler 全退（无 alias） |
| memory_relations.active/version/confidence | 列删除（migration 26 重建） |
| i_revision_memory_relations 'related' | 词表收口 related_to（数据迁移） |
| source 绑定 bind_confidence='revoked' | CHECK 收口 exact/high/low；revoked 行迁纠错历史 |
| deletion_requests action/resource_kind/ai_reason 等泛化列 | 表重建 Memory-only |
| deletion/service archive 分支+提示 | 删除（P-A01） |
| migration.py type=archived / archive/ 目录自动归档 | out_of_scope（正文不进报告，处置待裁定） |
| v1_compat deletion thin 层 | get 移正式注册；restore 删 |

## 新 schema 对照

- 新表：relation_corrections
- 改列：memory_relations / i_revision_memory_relations /
  plan_memory_links 补实例 ID；deletion_requests 重建；
  memories(fresh) CHECK 无 archived
- 迁移版本：formal 26（升级库数据迁移见报告 §1）

## 数据非零处置条件（§9.3）

- 生产库当前快照（2026-10-01 只读）：全部关系/删除表 **0 行或
  表不存在**（TABLE_MISSING 如实标注于 RELATION_FOLLOWUP）。
- 部署时门控（只读 COUNT）：archived memories、inactive/revoked
  关系、archive/letter 申请、旧 raw 前缀 source_ref——
  非零则按 §9.3 停线报 `LEGACY_TABLE_DATA_PRESENT`，本迁移代码
  已含对应转换（软删/revoked→纠错历史；archived 桶在迁移工具层
  out_of_scope 不入库）。
- 回滚：迁移 26 前备份快照（storage.backup）+ 分支整体放弃无损。

## 保留的历史迁移

letters DROP(24)、旧模块 DROP(25)、workspace(6) 等升级史迁移
保留不可变；fresh 初始化与本规范一致。
