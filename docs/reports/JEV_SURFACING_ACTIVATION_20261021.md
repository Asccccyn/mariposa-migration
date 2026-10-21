# JEV 自动浮现生产开启记录 · 2026-10-21

用户明确批准专用凭据创建、JEV当前/近邻对话与候选记忆数据许可，以及每日100次请求预算、名义1美元费用上限（金额计量不完整，不保证硬支出上限）。

## 当前生产入口

estomago: `/Users/owner/Documents/Codex/2026-10-21/jev/work/releases/estomago-jev-20261021`

该目录以先前正在运行的 effort UI 发布副本为底，仅叠加6个JEV源码/测试文件。前端与原发布副本逐字节相同，没有重建、替换资源版本或清CDN缓存。后续部署不能假定服务从 Projects/estomago 工作树启动，也不要删除此生产目录。

Mariposa 仍从 `/Users/owner/Projects/mariposa` 运行。

## 独立代码提交

- estomago `c0f31c2`：同轮15秒预算、编辑重发新修订任务。
- Mariposa `af2dbbe`：分阶段JEV校准、截止时间、严格恢复缺HTTP chunk结束块的完整响应。

未把其他工作树修改或草稿混入上述提交。未推送远端。

## 生效配置

- E `memory_surfacing.enabled=true`，总预算15000ms。
- M `MARIPOSA_SURFACE_ENABLED=1`，prepare10000ms、delivery4000ms。
- 准备同事件0.75，交付同事件0.80，当前相关性0.80；旧启动脚本0.50覆盖已移除。
- 专用 `binding_jiaming_surface` 仅允许 memory.surfacing.prepare/validate/check 和 memory.detail.read。
- 许可只增加 event_excerpt、structured_metadata、current_dialogue；保留原有主体和其他许可。
- 每日请求预算100，名义费用上限1美元；金额计量缺失时按请求预算回退，不能将其宣称为可靠美元硬上限。
- 凭据仅存本机私密surface.env，权限600，未进入Git或日志。

## 验证结果

两服务新进程的健康端点均200。使用实际E客户端和专用凭据调用M空候选prepare，返回empty、queries=0、reason=null、policy_revision=3、prompt=surface_prepare_v3，说明开关、授权及预算门已通过。探针未检索记忆、未发出JEV付费问句、未发出OR请求。

开启前后：messages与provider_requests计数完全相同；windows、window_trajectories、runtime_settings逐表哈希相同；jev_rerank_cache与jev_feature_cache计数和哈希相同。未换窗、未清缓存、未改旧前缀。既有外部缓存自然过期不在此校验范围。

候选发布版本typecheck及4文件相关测试通过，另有前序真实JEV与缓存回归证据。

## 回滚材料

启动脚本、原E配置、原判断许可行保存在各服务secrets目录的 `backups/jev-enable-20261021-v2/`，不在Git中。关闭浮现可先关闭两侧开关并正常重启；若撤销专用权限，只撤销该surface binding，不动聊天/归档/主动recall凭据。不要恢复整个聊天数据库或清空缓存来回滚。
