// v1.7：遗忘闭环已退役（2026-09-28）。本文件由正向遗忘 E2E 改为
// 退役负向守卫：旧入口 404、UI 无遗忘按钮。完整 E2E 待 /app/ 构建恢复。
import { readFileSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";

// 审计 2026-10-03：无效 Bearer 让一切 401，把认证失败当业务保护成功
// （恒绿）。改为读隔离实例种子 token，先证明认证通路成立（活跃能力
// 200/4xx 业务码），退役入口必须是 404 本身。
const __dir = dirname(fileURLToPath(import.meta.url));
const isoRoot = resolve(__dir, "../../../.pytest_tmp/e2e-isolated");

function ownerToken(): string {
  const data = JSON.parse(
    readFileSync(join(isoRoot, "runtime", "dev_tokens.json"), "utf-8"));
  return data.jiaming ?? data.qiaosheng;
}

test("authenticated smoke then retired endpoints 404", async ({ request }) => {
  const token = ownerToken();
  // 1) 认证通路成立：活跃能力给出非 401 的业务响应
  const alive = await request.post("/api/capability/time.context", {
    headers: { Authorization: `Bearer ${token}` },
    data: { arguments: {} },
  });
  expect(alive.status()).not.toBe(401);
  expect(alive.ok()).toBeTruthy();
  // 2) 退役入口：404（而不是被 401 提前拦截）
  for (const cap of ["workspace.forgetting.scan", "memory.forgetting.decide",
                     "memory.restore"]) {
    const r = await request.post(`/api/capability/${cap}`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { arguments: {} },
    });
    expect(r.status()).toBe(404);
  }
});
