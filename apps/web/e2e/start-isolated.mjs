// E2E 隔离服务启动（spec_v2 §16）：不复用业务库 18780 实例。
// 自建隔离根 .pytest_tmp/e2e-isolated（受 conftest 保险丝白名单保护），
// 种子 token 落在隔离根 runtime/；端口 18799 仅测试用。
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dir = dirname(fileURLToPath(import.meta.url));
const repo = resolve(__dir, "../../..");
const isoRoot = join(repo, ".pytest_tmp", "e2e-isolated");
const py = join(repo, ".venv", "Scripts", "python.exe");
const PORT = 18799;

// 清理上次异常退口的监听（pidfile 记录 uvicorn 子进程）
const pidFile = join(isoRoot, "uvicorn.pid");
if (existsSync(pidFile)) {
  const pid = parseInt(readFileSync(pidFile, "utf-8").trim(), 10);
  if (Number.isFinite(pid)) {
    try { process.kill(pid); } catch { /* 已退出 */ }
  }
  rmSync(pidFile, { force: true });
}

const env = {
  ...process.env,
  MARIPOSA_ROOT: isoRoot,
  PYTHONPATH: join(repo, "backend"),
  MARIPOSA_BIND: "127.0.0.1",
  MARIPOSA_PORT: String(PORT),
  // 不带语义 provider：E2E 不依赖模型，缺失时关键词路径照常
  MARIPOSA_SEMANTIC_PROVIDER: "",
};

// 1) 迁移 + 种子（隔离库 + 隔离 dev_tokens.json）
const seed = spawnSync(py, ["-m", "mariposa.devseed"], {
  env, cwd: repo, encoding: "utf-8", shell: false,
});
if (seed.status !== 0) {
  console.error("devseed failed:", seed.stdout, seed.stderr);
  process.exit(1);
}

// 2) uvicorn（同进程托管；收到退出信号时杀掉子进程）
const srv = spawn(py, [
  "-m", "uvicorn", "mariposa.app:app",
  "--host", "127.0.0.1", "--port", String(PORT),
], { env, cwd: repo, stdio: "inherit", windowsHide: true });

import { writeFileSync } from "node:fs";
writeFileSync(pidFile, String(srv.pid), "utf-8");

const kill = () => {
  try { srv.kill(); } catch { /* noop */ }
  try { rmSync(pidFile, { force: true }); } catch { /* noop */ }
};
process.on("SIGTERM", kill);
process.on("SIGINT", kill);
process.on("exit", kill);
srv.on("exit", (code) => process.exit(code ?? 0));
