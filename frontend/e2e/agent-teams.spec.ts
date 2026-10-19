import { expect, test } from "@playwright/test";
import { authenticatedRequest } from "./authenticated-request";

test.skip(process.env.E2E_AGENT_TEAMS !== "true", "需要显式启用真实 Agent Teams 验收及费用授权");
test.use({ actionTimeout: 30_000 });

test("Agent Teams: authenticated research, dependency handoff, report and shutdown", async ({ page }, testInfo) => {
  test.setTimeout(1_500_000);
  const sources = [
    "https://www.postgresql.org/docs/17/sql-vacuum.html",
    "https://www.postgresql.org/docs/17/sql-analyze.html",
  ];
  const question = "请用 Agent Teams 研究 PostgreSQL 17 官方文档。全程只创建并使用两名成员：vacuum-checker 负责 VACUUM 的作用及是否回收操作系统磁盘空间；analyze-checker 负责 ANALYZE 的作用及统计信息与查询规划的关系。先创建两个互相独立的研究任务，再创建 blockedBy 前两项的汇总任务，汇总任务复用两个任务的事实 requirement_ids，owner 必须填写已有 vacuum-checker 的成员 ID。复用成员完成汇总，不得新增或替换成员，不得创建 summarizer，三项任务全部完成前不得关闭这两名成员。每个任务分别填写简短 subject 和完整 description，成员先提交计划供 Lead 审核；由 Lead 明确通过后研究。成员分享发现，汇总任务通过 TaskGet 读取已接纳的前置结果。最终用中文输出不超过 700 字的对比报告，附官方引用，完成后正常关闭团队。只使用所选两个官方页面；不研究配置、性能基准或未被询问的功能。";
  await page.goto("/login");
  await page.getByLabel("工作邮箱").fill(process.env.E2E_ADMIN_EMAIL!);
  await page.getByLabel("密码", { exact: true }).fill(process.env.E2E_ADMIN_PASSWORD!);
  await page.getByRole("button", { name: "进入研究台" }).click();
  await expect(page).toHaveURL(/\/(research\/new|admin\/configurations)$/);
  if (page.url().endsWith("/admin/configurations")) await page.goto("/research/new");
  const api = authenticatedRequest(page);
  const response = await api.post("/api/research/runs", { data: {
    messages: [{ role: "user", content: question }],
    source_selection: { mode: "specific", sources: sources.map((url) => ({ type: "url", url })) },
    configurable: {
      enable_async_research: true, async_research_mode: "teams", team_execution_mode: "plan_approval",
      allow_clarification: false, enable_human_in_loop: true, enable_memory: false,
      max_concurrent_research_units: 2, max_researcher_iterations: 8, max_react_tool_calls: 15,
      max_run_cost_micro_usd: 2_000_000, run_deadline_seconds: 1200,
      quality_evaluation_enabled: true, quality_evaluation_fail_open: false,
      quality_evaluation_min_sources: 1, report_review_enabled: true,
      search_api: "none", search_providers: [], fetch_backend_order: ["local", "playwright"], external_extract_backends: [],
    },
  } });
  expect(response.ok(), await response.text()).toBeTruthy();
  const { run_id: runId } = await response.json();
  console.log(`Agent Teams E2E run: ${runId}`);
  await testInfo.attach("run-id", { body: runId, contentType: "text/plain" });
  let complete = false;
  let lastState: Record<string, unknown> = {};
  try {
    await page.goto(`/research/${runId}`);
    await expect.poll(async () => {
      const stateResponse = await api.get(`/api/research/runs/${runId}`);
      expect(stateResponse.ok()).toBeTruthy();
      const state = await stateResponse.json();
      lastState = state;
      expect(["failed", "cancelled"], JSON.stringify(state)).not.toContain(state.status);
      if (state.pending_security_approvals?.length) {
        // The session authorizes only the two official sources in this test.
        for (const approval of state.pending_security_approvals) {
          const data = JSON.stringify(approval);
          expect(data).toMatch(/postgresql\.org/);
        }
      }
      if (state.pending_human_action || state.pending_security_approvals?.length) {
        const dialog = page.getByRole("dialog");
        if (!(await dialog.isVisible())) await page.getByRole("button", { name: /审批中心/ }).click();
        const approve = page.getByRole("button", { name: "批准并继续", exact: true });
        const allow = page.getByRole("button", { name: /^(允许一次|允许此次访问)$/ });
        if (await approve.isVisible()) await approve.click();
        else if (await allow.isVisible()) await allow.click();
      }
      return state.status;
    }, { timeout: 1_300_000, intervals: [3000] }).toBe("completed");
    complete = true;
    const final = await (await api.get(`/api/research/runs/${runId}`)).json();
    const teamResponse = await api.get(`/api/research/runs/${runId}/team`);
    expect(teamResponse.ok(), await teamResponse.text()).toBeTruthy();
    const team = await teamResponse.json();
    expect(team.mode).toBe("teams");
    expect(team.status).toBe("closed");
    expect(team.tasks.length).toBeGreaterThanOrEqual(3);
    expect(team.members.filter((member: { member_id: string }) => member.member_id !== "lead")).toHaveLength(2);
    for (const task of team.tasks) {
      expect(task.subject.trim().length).toBeGreaterThan(0);
      expect(task.description.trim().length).toBeGreaterThan(0);
      expect(Array.isArray(task.blocks) && Array.isArray(task.blockedBy)).toBe(true);
      expect(task.status).toBe("completed");
      expect(["accepted", "accepted_with_caveats"]).toContain(task.admission_status);
      expect(task.unresolvedBlockedBy).toEqual([]);
    }
    expect(team.tasks.some((task: { blockedBy: string[] }) => task.blockedBy.length >= 2)).toBe(true);
    expect(team.plans.filter((plan: { status: string }) => plan.status === "approved").length).toBeGreaterThanOrEqual(3);
    expect(team.members.every((member: { status: string }) => member.status === "closed")).toBe(true);
    expect(final.output.markdown).toMatch(/postgresql\.org/);
    expect(final.output.report_review?.decision).toBe("pass");
    expect(final.output.report_review?.degraded).not.toBe(true);
    const approvalDialog = page.getByRole("dialog");
    if (await approvalDialog.isVisible()) await page.keyboard.press("Escape");
    await expect(approvalDialog).toBeHidden();
    await page.getByRole("tab", { name: "研究团队", exact: true }).click();
    await expect(page.locator(".team-task")).toHaveCount(team.tasks.length);
    await page.screenshot({ path: testInfo.outputPath("team-board.png"), fullPage: true });
    await page.reload();
    await page.getByRole("tab", { name: "报告", exact: true }).click();
    await expect(page.locator(".report-shell")).toContainText(/VACUUM/i);
    await page.screenshot({ path: testInfo.outputPath("report.png"), fullPage: true });
    const usage = await (await api.get(`/api/research/runs/${runId}/usage`)).json();
    expect(usage.totals.calls.attempts).toBeGreaterThan(0);
    await testInfo.attach("team-evidence", { body: JSON.stringify({ runId, team, usage, output: final.output }), contentType: "application/json" });
  } finally {
    await testInfo.attach("last-run-state", { body: JSON.stringify(lastState), contentType: "application/json" });
    if (!complete) await api.post(`/api/research/runs/${runId}/cancel`);
  }
});
