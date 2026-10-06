# mariposa 项目约定

## 测试运行硬性约束（2026-09-22 事故后生效）

2026-09-22，本项目的 pytest 进程树（`python -m pytest tests/unit/test_p1_fixes.py`）泄漏 40GB 内存，把宿主 commit 顶到上限，导致同机六个生产服务（DevSpace/Superposition/Bobo/Zashidele/Antinomy/Ombre）集体故障。此宿主还承载生产 Tunnel，测试失控即生产事故。

无论你是什么 agent（ZCode/codex/GLM/其他）或人工 shell，在本项目运行测试时遵守：

1. **禁止后台悬挂测试**：不要用 `&`、nohup、detached 方式留下无人监视的 pytest；命令必须前台等待完成或设置显式超时。
2. **优先最小范围**：`-x -q` + 指定具体测试文件/用例，不要无差别跑全量套件；全量套件必须分批。
3. **资源有界**：单条 pytest 命令预期私有内存 >2GB 或耗时 >20 分钟时，先向用户确认再运行。
4. **宿主硬性保险**：`D:\DevSpace\scripts\test-guard.ps1`（计划任务 DevSpaceTestGuard，常驻）会强制终止任何满足以下条件的测试进程树——树私有内存 ≥4GB、树存活 ≥90 分钟、或系统 commit ≥92% 且树 ≥2GB。这是最后防线，不要试图绕过；如果你的测试被它杀掉，说明测试本身有问题（泄漏/死循环），修测试，不要重跑撞线。
5. 重负载测试（模型加载、全量 e2e）建议放进 WSL/容器单独跑，不要占宿主内存。

## 并行工作交叉检查（2026-10-05 立规）

本仓可能多路代理同时改（ZCode/codex/GLM 等）。无论哪一路，每次提交前先 `git status` + `git log --oneline -3`：

- 发现非本路的新提交或未提交改动时，**禁用 `git add -A`/`git add .`**，只 add 自己明确修改的文件；
- 他人半成品不得卷进自己的提交；与他人改动同文件时先读现状再改；
- 提交信息只描述自己做的部分；落地后如工作树被他人推进，重跑相关测试再继续。

（背景：同日 estómago 仓 ba41c18 曾把另一路改到一半的 README 卷进提交，内容无损但归属交叉；本仓 debug 期多路进出，同风险。）

## 其他

- 本目录与 D:\DevSpace（服务管理）、D:\superposition、D:\bobo-bridge 等同机共存，任何长驻进程都要显式声明端口与生命周期。
