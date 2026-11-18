import { execFileSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";
import { authenticatedRequest } from "./authenticated-request";

test.skip(process.env.E2E_ADAPTIVE_RECOVERY !== "true", "需要本次独立部署和真实模型预算");
test.describe.configure({ mode: "serial" });

async function start(page: Page) {
  await page.goto("/login");
  await page.getByLabel("工作邮箱").fill(process.env.E2E_ADMIN_EMAIL!);
  await page.getByLabel("密码", { exact: true }).fill(process.env.E2E_ADMIN_PASSWORD!);
  await page.getByRole("button", { name: "进入研究台" }).click();
  await expect(page).toHaveURL(/\/(research\/new|admin\/configurations)$/);
  await page.goto("/research/new");
  const api = authenticatedRequest(page);
  const created = await api.post("/api/research/runs", { data: {
    messages: [{ role: "user", content: "使用一个研究任务。仅依据所选 PostgreSQL 17 官方文档，说明 VACUUM FULL 对空间和锁的要求。中文简要回答并引用原文。" }],
    source_selection: { mode: "specific", sources: [{ type: "url", url: "https://www.postgresql.org/docs/17/sql-vacuum.html" }] },
    configurable: { research_execution_strategy: "adaptive", enable_async_research: false,
      allow_clarification: false, enable_human_in_loop: true, enable_memory: false,
      run_deadline_seconds: 900, max_run_cost_micro_usd: 3_000_000,
      quality_evaluation_enabled: true, report_review_enabled: true },
  } });
  expect(created.ok(), await created.text()).toBe(true);
  const { run_id: runId } = await created.json();
  await page.goto(`/research/${runId}`);
  return { api, runId };
}

test("adaptive recovery: restart at outline approval preserves committed research", async ({ page }, testInfo) => {
  test.setTimeout(1_400_000);
  const { api, runId } = await start(page);
  let completed = false;
  try {
    await expect.poll(async () => {
      const parked = await (await api.get(`/api/research/runs/${runId}`)).json();
      expect(["failed", "cancelled"]).not.toContain(parked.status);
      if (parked.status === "awaiting_plan_approval") {
        expect((await api.post(`/api/research/runs/${runId}/human-actions/${parked.pending_human_action.action_id}`,
          { data: { action: "approve" } })).ok()).toBe(true);
      }
      return parked.status;
    }, { timeout: 1_000_000, intervals: [2000] }).toBe("awaiting_outline_approval");
    const parked = await (await api.get(`/api/research/runs/${runId}`)).json();
    const calls = parked.progress.task_items["main-research"].tool_call_count;
    const actionId = parked.pending_human_action.action_id;
    expect(process.env.E2E_MANAGED_STACK).toBe("true");
    const project = process.env.COMPOSE_PROJECT_NAME;
    expect(project).toBeTruthy();
    const id = execFileSync("docker", ["ps", "-q", "--filter", `label=com.docker.compose.project=${project}`,
      "--filter", "label=com.docker.compose.service=api"], { encoding: "utf8" }).trim();
    expect(id.split(/\s+/)).toHaveLength(1);
    execFileSync("docker", ["restart", "--time", "5", id], { timeout: 60_000 });
    await expect.poll(async () => {
      try {
        const response = await api.get(`/api/research/runs/${runId}`);
        return response.ok() ? (await response.json()).status : "restarting";
      } catch { return "restarting"; }
    }, { timeout: 120_000, intervals: [2000] }).toBe("awaiting_outline_approval");
    await page.reload();
    expect((await api.post(`/api/research/runs/${runId}/human-actions/${actionId}`, { data: { action: "approve" } })).ok()).toBe(true);
    await expect.poll(async () => (await (await api.get(`/api/research/runs/${runId}`)).json()).status,
      { timeout: 300_000, intervals: [2000] }).toBe("completed");
    completed = true;
    const final = await (await api.get(`/api/research/runs/${runId}`)).json();
    expect(final.output.completion_status).toBe("success");
    expect(final.output.report_review?.decision).toBe("pass");
    expect(final.progress.task_items["main-research"].tool_call_count).toBe(calls);
    expect(final.output.markdown).toContain("VACUUM");
    await testInfo.attach("recovery-evidence", { body: JSON.stringify({ runId, callsBeforeRestart: calls, final }), contentType: "application/json" });
  } finally {
    if (!completed) await api.post(`/api/research/runs/${runId}/cancel`);
  }
});

test("adaptive recovery: cancelling active main research reaches a terminal state", async ({ page }, testInfo) => {
  test.setTimeout(300_000);
  const { api, runId } = await start(page);
  try {
    await expect.poll(async () => {
      const snapshot = await (await api.get(`/api/research/runs/${runId}`)).json();
      if (snapshot.status === "awaiting_plan_approval") await api.post(`/api/research/runs/${runId}/human-actions/${snapshot.pending_human_action.action_id}`,
        { data: { action: "approve" } });
      return snapshot.progress.task_items?.["main-research"]?.status;
    }, { timeout: 180_000, intervals: [1000] }).toBe("running");
    expect((await api.post(`/api/research/runs/${runId}/cancel`)).ok()).toBe(true);
    await expect.poll(async () => (await (await api.get(`/api/research/runs/${runId}`)).json()).status,
      { timeout: 90_000, intervals: [1000] }).toBe("cancelled");
    expect((await (await api.get(`/api/research/runs/${runId}/team`)).json()).enabled).toBe(false);
    await page.reload();
    await expect(page.getByText("已取消", { exact: false }).first()).toBeVisible();
    await testInfo.attach("cancelled-run", { body: JSON.stringify({ runId }), contentType: "application/json" });
  } finally {
    await api.post(`/api/research/runs/${runId}/cancel`);
  }
});
