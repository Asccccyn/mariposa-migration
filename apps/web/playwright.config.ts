import { defineConfig } from "@playwright/test";

// E2E 一律打隔离实例（.pytest_tmp/e2e-isolated，端口 18799）：
// 绝不复用指向业务库的 18780 开发实例（spec_v2 §16）。
const E2E_BASE = "http://127.0.0.1:18799";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60000,
  retries: 0,
  use: {
    baseURL: E2E_BASE,
    headless: true,
  },
  reporter: [["list"]],
  webServer: {
    command: "node e2e/start-isolated.mjs",
    url: `${E2E_BASE}/health`,
    reuseExistingServer: false,
    timeout: 90_000,
    stdout: "ignore",
  },
});
