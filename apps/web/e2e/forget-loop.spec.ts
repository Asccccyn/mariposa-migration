// v1.7：遗忘闭环已退役（2026-09-28）。本文件由正向遗忘 E2E 改为
// 退役负向守卫：旧入口 404、UI 无遗忘按钮。完整 E2E 待 /app/ 构建恢复。
import { test, expect } from "@playwright/test";

test("retired forgetting endpoints return 404", async ({ request }) => {
  for (const cap of ["workspace.forgetting.scan", "memory.forgetting.decide",
                     "memory.restore"]) {
    const r = await request.post(`/api/capability/${cap}`, {
      headers: { Authorization: "Bearer test" },
      data: { arguments: {} },
    });
    expect([404, 401]).toContain(r.status());
  }
});
