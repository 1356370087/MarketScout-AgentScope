import { expect, test } from "@playwright/test";
import { interruptNativeRun } from "./native-faults";

// 仅在显式指定的真实原生部署执行；每个来源模式都必须提供真实可访问资料。
test.skip(process.env.E2E_NATIVE_FULL !== "true", "设置 E2E_NATIVE_FULL=true 执行真实模型与搜索验收");
test.describe.configure({ mode: "serial" });

for (const mode of ["web", "documents", "hybrid", "specific"]) {
  test(`native ${mode}: approvals, report, reload, usage and publication`, async ({ page }, testInfo) => {
    test.setTimeout(20 * 60_000);
    const selections = JSON.parse(process.env.E2E_SOURCE_SELECTIONS ?? "{}");
    const selection = selections[mode] ?? (mode === "web" ? { mode: "web" } : undefined);
    expect(selection, `E2E_SOURCE_SELECTIONS 缺少 ${mode} 的真实资料选择`).toBeTruthy();
    if (process.env.E2E_ADMIN_EMAIL && process.env.E2E_ADMIN_PASSWORD) {
      await page.goto("/login");
      await page.getByLabel("工作邮箱").fill(process.env.E2E_ADMIN_EMAIL);
      await page.getByLabel("密码").fill(process.env.E2E_ADMIN_PASSWORD);
      await page.getByRole("button", { name: "进入研究台" }).click();
      await expect(page).toHaveURL(/\/research\/new$/);
    } else {
      expect(process.env.NEXT_PUBLIC_LOCAL_DEV_AUTH_BYPASS).toBe("true");
    }
    const created = await page.request.post("/api/research/runs", { data: {
      messages: [{ role: "user", content: process.env.E2E_RESEARCH_QUESTION ?? "基于所选资料比较 PostgreSQL 17 与 16 的查询规划变化，给出可核验引用。" }],
      source_selection: selection,
      configurable: { allow_clarification: false, enable_human_in_loop: true, enable_async_research: true },
    } });
    expect(created.ok(), await created.text()).toBeTruthy();
    const { run_id: runId } = await created.json();
    let completed = false;
    let faultInjected = false;
    try {
      await page.goto(`/research/${runId}`);
      let snapshot;
      await expect.poll(async () => {
        const response = await page.request.get(`/api/research/runs/${runId}`);
        expect(response.ok()).toBeTruthy();
        snapshot = await response.json();
        expect(snapshot.engine).toBe("agentscope");
        expect(["failed", "cancelled"]).not.toContain(snapshot.status);
        const fault = process.env.E2E_FAULT_SCENARIO;
        if (fault && !faultInjected && snapshot.status === "running") {
          faultInjected = interruptNativeRun(runId, fault);
          if (faultInjected && fault === "api-gateway-kill") {
            // 等待旧租约自然过期，不修改 TTL、不清空 fence。
            await page.waitForTimeout(35_000);
            await expect.poll(async () => {
              try { return (await page.request.post(`/api/research/runs/${runId}/resume`, { data: {} })).status(); }
              catch { return 0; }
            }, { timeout: 90_000 }).toBe(202);
            await page.reload();
          }
        }
        if (snapshot.pending_human_action || snapshot.pending_security_approvals?.length) {
          const dialog = page.getByRole("dialog");
          if (!(await dialog.isVisible())) await page.getByRole("button", { name: /审批中心/ }).click();
          const approve = page.getByRole("button", { name: "批准并继续", exact: true });
          const allow = page.getByRole("button", { name: "允许一次", exact: true });
          if (await approve.isVisible()) await approve.click();
          else if (await allow.isVisible()) await allow.click();
        }
        return snapshot.status;
      }, { timeout: 15 * 60_000, intervals: [2000] }).toBe("completed");
      completed = true;
      if (process.env.E2E_FAULT_SCENARIO) expect(faultInjected, "故障窗口没有触发，不能记为通过").toBe(true);
      const final = await (await page.request.get(`/api/research/runs/${runId}`)).json();
      expect(final.output.markdown.length).toBeGreaterThan(100);
      expect(final.output.markdown).toMatch(/https?:\/\/|local:\/\/|\/documents\//);
      await page.reload();
      await page.getByRole("tab", { name: "报告", exact: true }).click();
      await expect(page.locator(".report-shell")).toContainText(final.output.markdown.replace(/^#+\s*/gm, "").slice(0, 15));
      const usageResponse = await page.request.get(`/api/research/runs/${runId}/usage`);
      expect(usageResponse.ok()).toBeTruthy();
      const usage = await usageResponse.json();
      expect(usage.totals.calls.attempts).toBeGreaterThan(0);
      const publicationResponse = await page.request.post(`/api/research/runs/${runId}/publications`, { data: { format: "markdown" } });
      expect(publicationResponse.ok(), await publicationResponse.text()).toBeTruthy();
      const publication = await publicationResponse.json();
      await expect.poll(async () => (await (await page.request.get(`/api/research/runs/${runId}/publications/${publication.publication_id}`)).json()).status,
        { timeout: 120_000 }).toBe("completed");
      const download = await page.request.get(`/api/research/runs/${runId}/publications/${publication.publication_id}/download`);
      expect(download.ok()).toBeTruthy();
      expect((await download.body()).length).toBeGreaterThan(100);
      await testInfo.attach("native-e2e-evidence", { body: JSON.stringify({ runId, mode, engine: final.engine, usage, publication }), contentType: "application/json" });
    } finally {
      if (!completed) await page.request.post(`/api/research/runs/${runId}/cancel`);
    }
  });
}
