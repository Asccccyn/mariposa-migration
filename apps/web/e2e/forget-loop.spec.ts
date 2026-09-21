import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect, request, type Page } from "@playwright/test";

// React 版（/app，构建产物同源 /api）浏览器级遗忘闭环。
// 前置：后端 18780 运行中、apps/web 已 npm run build。

const __dir = dirname(fileURLToPath(import.meta.url));
const tokens = JSON.parse(readFileSync(
  resolve(__dir, "../../../runtime/dev_tokens.json"), "utf-8")) as Record<string, string>;

async function login(page: Page, who: string) {
  await page.goto("/app/");
  await page.selectOption("header select", who);
  await page.fill("header input", tokens[who]);
  await page.click("header button.primary");
  await page.waitForTimeout(1500); // 等 reload + 初始请求完成
  await page.waitForSelector("nav button", { timeout: 15000 });
}

async function doSearch(page: Page, query: string) {
  await page.fill("main input", query);
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await page.waitForTimeout(600);
}

async function resetForgottenToFull() {
  const ctx = await request.newContext({
    baseURL: "http://127.0.0.1:18780",
    extraHTTPHeaders: { Authorization: `Bearer ${tokens["qiaosheng"]}` },
  });
  const out = await (await ctx.post("/api/capability/memory.search", {
    data: { arguments: { query: "心爱的小物" } } })).json();
  for (const h of out.data?.hits ?? []) {
    const got = await (await ctx.post("/api/capability/memory.get", {
      data: { arguments: { memory_id: h.memory_id } } })).json();
    if (got.data?.representation === "forgotten_summary") {
      await ctx.post("/api/capability/memory.restore", {
        data: { arguments: { memory_id: h.memory_id,
          expected_current_version: got.data.version } } });
    }
  }
  // pin 住全部历史候选桶，保证本轮新桶是唯一 scan 候选（batch=20）
  const all = await (await ctx.post("/api/capability/memory.list",
    { data: { arguments: { limit: 100 } } })).json();
  for (const m of all.data?.items ?? []) {
    if (!m.pinned) {
      await ctx.post("/api/capability/memory.pin",
        { data: { arguments: { memory_id: m.memory_id, value: true } } });
    }
  }
  await ctx.dispose();
}

test("浏览器级遗忘闭环：写桶→扫描→提案→审批→摘要切换→恢复", async ({ page }) => {
  await resetForgottenToFull(); // 干净起点：恢复历史残留遗忘桶
  const magicWord = `蓝瓷小钥匙E2E${Date.now() % 100000}`;
  await login(page, "qiaosheng");

  // 1. 写测试记忆（window.prompt 由 dialog 处理器填入）；以第 2 步搜索命中为功能断言
  page.once("dialog", (d) => d.accept(`我们把${magicWord}藏进了书架第三层。`));
  await page.getByRole("button", { name: "写测试记忆" }).click();
  await page.waitForTimeout(1200);

  // 2. 搜索命中；记下本轮桶的 memory_id 用于后续锚定
  await doSearch(page, magicWord);
  await expect(page.locator(".item")).toContainText(magicWord);
  await expect(page.locator(".item .tag.full")).toBeVisible();
  const memId = (await page.locator(".item .meta span").first().textContent())!.trim();

  // 3. 扫描候选（40 天前 > 30 天门槛）
  await page.getByRole("button", { name: "扫描遗忘候选" }).click();
  await page.waitForTimeout(800);

  // 4. 工作区：锚定本轮桶的草稿，填摘要并提交
  await page.getByRole("button", { name: "工作区 / 审批" }).click();
  const draft = page.locator(".item", { hasText: memId }).first();
  await draft.locator("textarea").first()
    .fill("把一件心爱的小物收进了书架的盒子里（E2E 摘要）。");
  await draft.locator("textarea").nth(1).fill("E2E 琐事压缩");
  await draft.getByRole("button", { name: "保存修订" }).click();
  await page.waitForTimeout(800);
  const ready = page.locator(".item", { hasText: memId }).first();
  await ready.getByRole("button", { name: "提交" }).click();
  await page.waitForTimeout(800);

  // 5. 审批（锚定本轮桶的 submitted 项）
  const submitted = page.locator(".item", { hasText: memId }).first();
  await submitted.getByRole("button", { name: "批准" }).click();
  await page.waitForTimeout(1000);

  // 6. 旧词不命中；摘要词命中且标记遗忘摘要
  await page.getByRole("button", { name: "记忆 / 检索" }).click();
  await page.waitForTimeout(800);
  await doSearch(page, magicWord);
  await expect(page.locator(".item")).toHaveCount(0, { timeout: 10000 });
  await doSearch(page, "心爱的小物");
  await expect(page.locator(".item .tag.forgotten_summary").first()).toBeVisible();

  // 7. 恢复后旧词重新命中（入口在摘要词结果里；本测试的桶是最新审批的一个）
  await page.getByRole("button", { name: "恢复旧正文" }).first().click();
  await page.waitForTimeout(1500);
  await doSearch(page, magicWord);
  await expect(page.locator(".item").first()).toContainText(magicWord);
});

test("日历页签渲染月视图", async ({ page }) => {
  await login(page, "qiaosheng");
  await page.getByRole("button", { name: "日历" }).click();
  await expect(page.locator("table.cal")).toBeVisible();
  await page.locator("table.cal td").first().click(); // 不崩溃即可
});
