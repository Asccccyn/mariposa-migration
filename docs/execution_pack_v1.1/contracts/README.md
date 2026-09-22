# 契约文件说明

本目录是给 GLM 的实现输入，不是已部署服务器。任何 `not_started` 都不能显示成已实现。

`capabilities.v1.json`：150项最低能力清单；每项有canonical/MCP名称、HTTP路径、角色、scope、读写类型、幂等/版本要求、阶段和外部依赖标记。其中7项一起听歌能力为明确预留。实际下游API映射必须现场核验。

`core_input_schemas.json`：12个核心工具的严格JSON Schema（Draft 2020-12），针对arguments。角色/绑定不能作为arguments自报。它不替代后端跨字段约束、认证或事务检查，也不是所有150项能力的完整OpenAPI。其余输入/全部输出schema由GLM在实现时补齐并测试。

`core_examples.json`：12个合成输入样例。全零hash只满足格式，不是真正可批准的payload校验值；实体ID也不是生产资源。不能拿示例当线上审批证据。

`acceptance_cases.json`：118个未执行的验收规格；与02文档对应。实现者必须追加实际测试定位与结果，不改原预期掩盖失败。

`.env.example`：默认配置模板，无真实密钥。它不是已核验环境，空鉴权配置不允许无认证上线。平台CLI版本/登录由Phase 0核验，不把环境键误当CLI flag。

主文档优先约束语义；本清单的legacy/provider能力只有核验真实接口后才能启用。工具可发现性不等于权限；所有handler必须再做resource guard。批准身份由认证上下文决定，不存在客户端能填写的 `actor=jiaming` 捷径。
