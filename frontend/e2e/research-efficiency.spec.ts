import { expect, test } from "@playwright/test";

// 在已完成的真实运行上检查前端；不重复触发模型，也不使用伪造 HTTP 数据。
test.skip(!process.env.E2E_EFFICIENCY_RUN_ID, "指定真实、已完成的效率验收运行 ID");

test("efficiency: progress, usage, report and reload use durable evidence", async ({ page }, testInfo) => {
  const runId = process.env.E2E_EFFICIENCY_RUN_ID!;
  const response = await page.request.get(`/api/research/runs/${runId}`);
  expect(response.ok()).toBeTruthy();
  const run = await response.json();
  expect(run.status).toBe("completed");
  expect(run.progress.sources.length).toBeGreaterThan(0);
  await page.goto(`/research/${runId}`);
  await page.getByRole("tab", { name: "研究进展", exact: true }).click();
  const progress = page.getByRole("region", { name: "资料检查与研究收敛" });
  await expect(progress).toBeVisible();
  await expect(progress).toContainText("交接通过");
  await expect(progress).toContainText("已准入证据");
  const counts = await progress.textContent();
  await page.reload();
  await page.getByRole("tab", { name: "研究进展", exact: true }).click();
  await expect(progress).toHaveText(counts!);
  await page.screenshot({ path: testInfo.outputPath("efficiency-progress.png"), fullPage: true });
  await page.getByRole("tab", { name: "用量", exact: true }).click();
  await expect(page.getByRole("heading", { name: "研究用量监控" })).toBeVisible();
  await page.getByRole("tab", { name: "按用途", exact: true }).click();
  await expect(page.locator(".usage-table").last()).toContainText("调用");
  await expect(page.locator(".usage-kpis")).not.toContainText("运行中");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(1200); // 等待图表首帧动画结束，仅用于截图。
  await page.screenshot({ path: testInfo.outputPath("efficiency-usage.png"), fullPage: true });
  await page.getByRole("tab", { name: "报告", exact: true }).click();
  await expect(page.locator(".report-shell")).toContainText("PostgreSQL");
  await page.screenshot({ path: testInfo.outputPath("efficiency-report.png"), fullPage: true });
});


test("efficiency: live inspection survives SSE reconnect", async ({ page, context }, testInfo) => {
  test.skip(!process.env.E2E_ACTIVE_EFFICIENCY_RUN_ID, "指定仍在执行的优化运行 ID");
  test.setTimeout(180_000);
  const runId = process.env.E2E_ACTIVE_EFFICIENCY_RUN_ID!;
  await page.goto(`/research/${runId}`);
  await page.getByRole("tab", { name: "研究进展", exact: true }).click();
  const panel = page.getByRole("region", { name: "资料检查与研究收敛" });
  await expect(panel).toContainText(/已获取 [1-9]\d* 份资料/, { timeout: 160_000 });
  const before = await (await page.request.get(`/api/research/runs/${runId}`)).json();
  await page.screenshot({ path: testInfo.outputPath("efficiency-live.png"), fullPage: true });
  await context.setOffline(true);
  await context.setOffline(false);
  await page.reload();
  await page.getByRole("tab", { name: "研究进展", exact: true }).click();
  await expect(panel).toContainText("已检查");
  const after = await (await page.request.get(`/api/research/runs/${runId}`)).json();
  expect(after.progress.efficiency.processed_chunks).toBeGreaterThanOrEqual(before.progress.efficiency.processed_chunks);
  expect(new Set(Object.keys(after.progress.task_items)).size).toBe(Object.keys(after.progress.task_items).length);
});
