import { expect, test } from "@playwright/test";
import { authenticatedRequest } from "./authenticated-request";

test.skip(process.env.E2E_ADAPTIVE_RESEARCH !== "true", "需要真实模型、独立验收库与费用预算");
test.describe.configure({ mode: "serial" });

const sources = [
  "https://www.postgresql.org/docs/17/sql-vacuum.html",
  "https://www.postgresql.org/docs/17/sql-analyze.html",
];
type Scenario = { name: string; strategy: "adaptive" | "delegated"; question: string; urls: string[];
  sourceMode?: "web" | "documents" | "hybrid"; documentId?: string };
const cases: Scenario[] = [
  { name: "direct", strategy: "adaptive", question: "使用一个研究任务。先读取 source_verification 技能，再仅依据所选 PostgreSQL 17 官方页面，核实普通 VACUUM 是否将空间归还操作系统，以及 VACUUM FULL 的区别。给出不超过 500 字的中文结论和可核验引用。", urls: sources.slice(0, 1) },
  { name: "delegation", strategy: "adaptive", question: "主研究直接核实 PostgreSQL 17 的 VACUUM 如何回收空间；将 ANALYZE 如何影响查询规划委派给一个独立子研究员。主研究收到结果后综合两项发现。只使用所选两页官方文档，不扩展到性能基准。输出不超过 700 字的中文比较与引用。", urls: sources },
  { name: "legacy", strategy: "delegated", question: "使用一个研究任务。仅依据所选 PostgreSQL 17 官方页面，核实普通 VACUUM 是否将空间归还操作系统，以及 VACUUM FULL 的区别。输出不超过 500 字的中文结论和引用。", urls: sources.slice(0, 1) },
];
if (process.env.E2E_ADAPTIVE_SOURCES === "true") {
  cases.push({ name: "web", strategy: "adaptive", sourceMode: "web", urls: [],
    question: "仅使用 PostgreSQL 17 官方文档，先检索 VACUUM 的相关说明，核实普通 VACUUM 与 VACUUM FULL 在空间归还和锁方面的区别。中文回答，附可核验官方引用，不研究其他版本或性能基准。" });
  const documentId = process.env.E2E_DOCUMENT_ID;
  if (documentId) cases.push(
    { name: "documents", strategy: "adaptive", sourceMode: "documents", documentId, urls: [],
      question: "使用一个研究任务。仅根据所选运维说明，列出 Acme 的 PostgreSQL 17 维护窗口、日常维护命令，以及 VACUUM FULL 的审批要求。中文回答并引用所选文档，不进行网络检索。" },
    { name: "hybrid", strategy: "adaptive", sourceMode: "hybrid", documentId, urls: [],
      question: "使用一个研究任务。依据所选运维说明确定 Acme 的维护窗口与 VACUUM FULL 审批要求，并查阅 https://www.postgresql.org/docs/17/sql-vacuum.html 核实 VACUUM FULL 是否需要额外磁盘空间和独占锁。区分内部排期和官方技术事实，中文回答并分别引用两类资料。" },
  );
}

for (const scenario of cases) {
  test(`adaptive research: ${scenario.name}`, async ({ page }, testInfo) => {
    test.setTimeout(1_500_000);
    await page.goto("/login");
    await page.getByLabel("工作邮箱").fill(process.env.E2E_ADMIN_EMAIL!);
    await page.getByLabel("密码", { exact: true }).fill(process.env.E2E_ADMIN_PASSWORD!);
    await page.getByRole("button", { name: "进入研究台" }).click();
    await expect(page).toHaveURL(/\/(research\/new|admin\/configurations)$/);
    await page.goto("/research/new");
    await page.getByRole("button", { name: "研究偏好", exact: true }).click();
    await expect(page.getByRole("radio", { name: /自适应研究/ })).toBeVisible();
    await page.keyboard.press("Escape");
    const api = authenticatedRequest(page);
    const created = await api.post("/api/research/runs", { data: {
      messages: [{ role: "user", content: scenario.question }],
      source_selection: { mode: scenario.sourceMode ?? "specific", sources: scenario.documentId
        ? [{ type: "document", id: scenario.documentId }] : scenario.urls.map(url => ({ type: "url", url })) },
      configurable: {
        quality_evaluation_min_sources: scenario.sourceMode === "web" ? 3 : scenario.sourceMode === "hybrid" ? 2 : Math.max(1, scenario.urls.length),
        research_execution_strategy: scenario.strategy, enable_async_research: false,
        allow_clarification: false, enable_human_in_loop: true, enable_memory: false,
        max_concurrent_research_units: 2, max_react_tool_calls: 10,
        max_run_cost_micro_usd: 3_000_000, run_deadline_seconds: 1200,
        quality_evaluation_enabled: true, quality_evaluation_fail_open: false,
        report_review_enabled: true, report_review_fail_open: false,
      },
    } });
    expect(created.ok(), await created.text()).toBe(true);
    const { run_id: runId } = await created.json();
    console.log(`Adaptive E2E ${scenario.name}: ${runId}`);
    let completed = false;
    try {
      await page.goto(`/research/${runId}`);
      await expect.poll(async () => {
        const snapshot = await (await api.get(`/api/research/runs/${runId}`)).json();
        expect(["failed", "cancelled"], JSON.stringify(snapshot.error ?? snapshot)).not.toContain(snapshot.status);
        if (snapshot.pending_human_action || snapshot.pending_security_approvals?.length) {
          for (const approval of snapshot.pending_security_approvals ?? []) {
            expect(["www.postgresql.org", "postgresql.org", "api.deepseek.com"]).toContain(approval.target?.domain);
          }
          if (!await page.getByRole("dialog").isVisible()) await page.getByRole("button", { name: /审批中心/ }).click();
          // Live model runs wait for the human monitoring this browser.
        }
        return snapshot.status;
      }, { timeout: 1_350_000, intervals: [2000] }).toBe("completed");
      completed = true;
      const final = await (await api.get(`/api/research/runs/${runId}`)).json();
      expect(final.output.completion_status).toBe("success");
      expect(final.output.report_review?.decision).toBe("pass");
      const tasks = Object.values(final.progress.task_items ?? {}) as Array<{ task_id: string; mode?: string }>;
      expect(final.progress.sources.length).toBeGreaterThan(0);
      if (scenario.strategy === "adaptive") expect(tasks.some(task => task.task_id === "main-research")).toBe(true);
      if (scenario.strategy === "adaptive") expect(final.progress.efficiency.tasks["main-research"].status).toBe("completed");
      if (scenario.name === "direct") expect(tasks.filter(task => task.mode === "adaptive-delegated")).toHaveLength(0);
      if (scenario.name === "delegation") expect(tasks.filter(task => task.mode === "adaptive-delegated")).toHaveLength(1);
      if (scenario.name === "delegation") expect(final.progress.sources.map((source: { task_id?: string }) => source.task_id))
        .toContain(tasks.find(task => task.mode === "adaptive-delegated")?.task_id);
      expect(final.output.markdown).toContain(scenario.sourceMode === "documents" ? "/documents/" : "postgresql.org/docs/17/");
      if (scenario.sourceMode === "hybrid") expect(final.output.markdown).toContain("/documents/");
      expect(final.output.markdown).toContain("VACUUM");
      expect(final.output.degraded).not.toBe(true);
      const activities = await Promise.all(tasks.map(async task => ({
        taskId: task.task_id, activity: await (await api.get(`/api/research/runs/${runId}/tasks/${task.task_id}/activity?limit=200`)).json(),
      })));
      if (scenario.name === "direct") expect(JSON.stringify(activities)).toContain("ReadSkill");
      const usage = await (await api.get(`/api/research/runs/${runId}/usage`)).json();
      expect(usage.totals.calls.attempts).toBeGreaterThan(0);
      await page.reload();
      await page.getByRole("tab", { name: "报告", exact: true }).click();
      await expect(page.locator(".report-shell")).toContainText("VACUUM");
      await page.screenshot({ path: testInfo.outputPath("report.png"), fullPage: true });
      const published = await api.post(`/api/research/runs/${runId}/publications`, { data: { format: "markdown" } });
      expect(published.ok(), await published.text()).toBe(true);
      const publication = await published.json();
      await expect.poll(async () => (await (await api.get(`/api/research/runs/${runId}/publications/${publication.publication_id}`)).json()).status,
        { timeout: 120_000 }).toBe("completed");
      const download = await api.get(`/api/research/runs/${runId}/publications/${publication.publication_id}/download`);
      expect(download.ok()).toBe(true);
      expect((await download.body()).length).toBeGreaterThan(100);
      await testInfo.attach("adaptive-evidence", { body: JSON.stringify({ runId, scenario: scenario.name, final, usage, activities, publication }), contentType: "application/json" });
    } finally {
      if (!completed) await api.post(`/api/research/runs/${runId}/cancel`);
    }
  });
}
