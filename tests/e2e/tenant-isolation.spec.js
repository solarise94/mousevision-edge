/**
 * 租户隔离 Chromium E2E（合同 §15-B6 Done / §16-G6）。
 *
 * 拓扑（tests/e2e/seed_and_run.py 种子）：
 *   account A（parent-a）：Workspace A1（op-a1 operator；记录 e2e-a1-1/e2e-a1-2，笼 C57-101）
 *                        Workspace A2（记录 e2e-a2-1，笼 C57-202）
 *   account B：           Workspace B1（记录 e2e-b1-1，笼 C57-301）
 *
 * 场景：
 *   ① parent 登录看到已绑定 A1/A2；未绑定 B1 的名称/数据不出现
 *   ② 子账号 A1 登录（自动激活）看不到 A2/B1 的任何名称与数据
 *   ③ 深链直访他租户资源（记录详情/照片 URL）被拒（403/404）且 UI 不泄露存在性
 *   ④ 页面进入终态、无永久 loading（data-loading 标记轮询断言）
 *   ⑤ 租户切换后数据集变化正确（A1 ↔ A2）
 */
const { test, expect } = require("@playwright/test");

const PARENT = { username: "parent-a", password: "e2e-parent-password" };
const CHILD = { username: "op-a1", password: "e2e-operator-password" };

// 与 seed_and_run.py 保持一致（控制面写死 seed UUID 的 legacy-default 除外，
// 业务租户 UUID 随机，这里经 UI 的 DOM data 属性解析）。
async function login(page, { username, password }) {
  await page.goto("/pc");
  await page.getByPlaceholder("用户名").fill(username);
  await page.getByPlaceholder("密码").fill(password);
  await page.getByRole("button", { name: "登录" }).click();
}

async function workspaceIds(page) {
  return page.$$eval('[data-testid="workspace-card"]', (cards) =>
    cards.map((c) => c.getAttribute("data-tenant-id"))
  );
}

test.describe("租户隔离（parent / 子账号 / 深链）", () => {
  // ① parent 看已绑定 A1/A2、不看未绑定 B1（§15-B6 Done 第一条）
  test("parent 登录看到已绑定的 A1/A2，未绑定的 B1 不出现", async ({ page }) => {
    await login(page, PARENT);
    // 终态：工作区总览页渲染出恰好两张工作区卡片
    const grid = page.getByTestId("workspace-grid");
    await expect(grid).toBeVisible();
    await expect(page.getByTestId("workspace-card")).toHaveCount(2);
    const names = await page.locator(".workspace-name").allTextContents();
    expect(names).toContain("Workspace A1");
    expect(names).toContain("Workspace A2");
    // B1（未绑定 account）在整页任何位置都不出现：名称、计数、可猜链接
    const html = await page.content();
    expect(html).not.toContain("Workspace B1");
    // B1 的种子记录只经 b1 的 record_id 出现；页面无任何 b1 数据痕迹
    expect(html).not.toContain("e2e-b1-1");
  });

  // ② 子账号 A1 看不到 A2/B1 的任何名称与数据
  test("子账号 A1 看不到 A2/B1 的名称与数据", async ({ page }) => {
    await login(page, CHILD);
    // 单租户成员登录自动激活 → 直接落数据页并渲染终态
    await expect(page.getByTestId("current-workspace")).toContainText("Workspace A1");
    await expect(page.locator(".cage-row-head").first()).toBeVisible();
    const html = await page.content();
    expect(html).not.toContain("Workspace A2");
    expect(html).not.toContain("Workspace B1");
    // A2/B1 的数据值（笼号/record_id）不泄露
    expect(html).not.toContain("C57-202");
    expect(html).not.toContain("C57-301");
    expect(html).not.toContain("e2e-a2-1");
    expect(html).not.toContain("e2e-b1-1");
    // 工作区切换器只有自己的 A1 一项
    const opts = await page.locator('[data-testid="tenant-switcher"] option').allTextContents();
    expect(opts).toEqual(["Workspace A1"]);
    // 子账号没有主账号 account 入口（API 403，UI 不渲染）
    expect(html).not.toContain("工作区总览");
  });

  // ③ 深链直访他租户资源被拒且 UI 不泄露存在性（§15-B6 Done 第三条）
  test("深链直访他租户资源被拒（403/404）且不泄露存在性", async ({ page }) => {
    await login(page, CHILD);
    await expect(page.getByTestId("current-workspace")).toContainText("Workspace A1");

    // 子账号没有汇总页：工作区卡片集合为空（存在性不泄露）
    const a1Card = await workspaceIds(page);
    expect(a1Card).toEqual([]);

    // page.request 与浏览器上下文共享会话 Cookie（登录态 A1）。
    // 他租户记录详情：A1 会话 + 他租户 record_id → 统一 404（不泄露存在性）
    const detailA2 = await page.request.get("/api/records/e2e-a2-1");
    expect([403, 404]).toContain(detailA2.status());
    const detailB1 = await page.request.get("/api/records/e2e-b1-1");
    expect([403, 404]).toContain(detailB1.status());
    // 他租户照片/视频深链同样拒绝
    const photoB1 = await page.request.get("/api/records/e2e-b1-1/photo");
    expect([403, 404]).toContain(photoB1.status());
    // 本租户记录正常可达（对照：拒绝不是全局性故障）
    const own = await page.request.get("/api/records/e2e-a1-1");
    expect(own.status()).toBe(200);
    // 拒绝响应体不回显跨租户信息
    const body = await detailB1.text();
    expect(body).not.toContain("C57-301");
    expect(body).not.toContain("Workspace");
  });

  // ④ 页面进入终态、无永久 loading（§16-G6）
  test("页面进入终态且无永久 loading", async ({ page }) => {
    await login(page, PARENT);
    const grid = page.getByTestId("workspace-grid");
    await expect(grid).toBeVisible();
    // data-loading 已清除（loadRoute finally）
    await expect(page.locator('#app[data-loading="1"]')).toHaveCount(0);
    // 轮询 3 秒：终态稳定，不出现回弹的永久 loading
    const stillTerminal = await page.evaluate(async () => {
      for (let i = 0; i < 6; i++) {
        await new Promise((r) => setTimeout(r, 500));
        const app = document.getElementById("app");
        if (!app || app.hasAttribute("data-loading")) return false;
        if (!document.querySelector('[data-testid="workspace-grid"]')) return false;
      }
      return true;
    });
    expect(stillTerminal).toBe(true);
    // 子账号路径同样终态收敛
    await page.getByRole("button", { name: "退出" }).click();
    await login(page, CHILD);
    await expect(page.locator(".cage-row-head").first()).toBeVisible();
    await expect(page.locator('#app[data-loading="1"]')).toHaveCount(0);
  });

  // ⑤ 租户切换后数据集变化正确
  test("parent 在 A1/A2 间切换后数据集随之变化", async ({ page }) => {
    await login(page, PARENT);
    const grid = page.getByTestId("workspace-grid");
    await expect(grid).toBeVisible();

    // 从汇总页卡片「进入」A1（主账号只读语义）
    const cards = page.getByTestId("workspace-card");
    const a1Card = cards.filter({ hasText: "Workspace A1" });
    await a1Card.locator('[data-testid="workspace-enter"]').click();
    await expect(page.getByTestId("current-workspace")).toContainText("Workspace A1");
    await expect(page.locator(".cage-row-head").first()).toBeVisible();
    await expect(page.locator("body")).toContainText("C57-101");
    await expect(page.locator("body")).not.toContainText("C57-202");
    // 展开笼行：A1 两只（11.11g / 12.50g）；页面无他租户的 22.22
    await page.locator(".cage-row-head").first().click();
    await expect(page.locator(".cage-thumb").first()).toBeVisible();
    const htmlA1 = await page.content();
    expect(htmlA1).toContain("11.11");
    expect(htmlA1).toContain("12.50");
    expect(htmlA1).not.toContain("22.22");

    // 经切换器切到 A2：数据集整体变化
    const switcher = page.getByTestId("tenant-switcher");
    const a2Id = await switcher.evaluate((sel) => {
      const opt = Array.from(sel.options).find((o) => o.textContent.startsWith("Workspace A2"));
      return opt ? opt.value : null;
    });
    expect(a2Id).toBeTruthy();
    await switcher.selectOption(a2Id);
    await expect(page.getByTestId("current-workspace")).toContainText("Workspace A2");
    await expect(page.locator(".cage-row-head").first()).toBeVisible();
    await expect(page.locator("body")).toContainText("C57-202");
    await expect(page.locator("body")).not.toContainText("C57-101");
    // parent 只读：不渲染写按钮（修正体重/发布等写入口）
    const htmlA2 = await page.content();
    expect(htmlA2).not.toContain("确认体重");
    expect(htmlA2).not.toContain("整笼通过");
  });
});
