# Relation/Deletion 验收记录（2026-10-01，规格 v2.0）

## 执行约束声明

- 真实 embedding/Jev/LLM 调用：**0**（本轮未运行
  test_dense_real_model_smoke——上轮越界不作为先例）
- 网络下载/收费调用：0；模型加载：0
- 测试全部前台小批次（≤17 文件/进程）、互斥分批、collect 口径对账

## 验收矩阵覆盖（§11，tests/unit/test_relation_deletion_acceptance.py
+ 专项文件）

| 组 | 覆盖节点 | 结果 |
|---|---|---|
| C01/C02/C03/C06 | archive 参数 schema 拒绝；fresh schema 无 archived/无效列/有 relation_corrections；detach/revoke/restore 退场+correct 可达；D01-D13 零复活 | 4 passed |
| R01/R02+R03/R08/R09/R11 | 五域纠错（memory/I/plan 实测；source/word 见专项）；Plan 状态往返与时间不解绑；重复建边幂等；纠错后重建新实例；同 key 重放不撤新边；最后 plan link 纠错→GAP | 6 passed |
| Q04/Q05/Q06/Q07 | 方向文字不颠倒；多分支全达；环有界；历史不混入有效关系 | 4 passed |
| D01..D16 | 空白理由/双 pending/5 次/撤回不返还/幂等重放/拒绝必理由/approve 静默/直删无理由/仅 jiaming/额度不束缚直删/五域阻断（入出边实测，其余 service 层单测）/纠错后可删+pending 保留/历史幸存 | 14 passed |
| T05/T08/T09 | atomic_write 同事务回执（direct delete 重放实测）；申请表无 live FK；fresh/upgrade 迁移（v25 隔离库 legacy 软删/revoked → corrections，幂等重迁移） | 通过（脚本级，见报告） |

## 专项补充节点

- R06 source 纠错：tests/unit/test_source_layer.py::
  TestBinding::test_correction_moves_to_history
- Q04 桶间方向/trace：tests/unit/test_round4.py::TestRelations
- T02 并发 decide：tests/unit/test_v17_audit_p1_deletion.py（线程）
- **R07 words 纠错**：service 层 correct_source 已实现（预期指纹
  CAS + source_msg: 校验 + 旧前缀拒绝）——公开 Registry 路径断言
  **未写**（如实申报，下批补）

## 全量结果

- unit：**569 collected = 568 passed + 1 skipped**（三互斥批）
- acceptance + integration（不含 real smoke）：100 passed
- 合计 **668 passed / 1 skipped / 0 failed**；真实模型 0 调用

## 已知未执行（如实）

- T03（approve×直删竞态）、T04（五域建边×删除跨域竞态线程矩阵）
- 前端构建/浏览器 E2E（依赖未装）
- 生产库零接触
