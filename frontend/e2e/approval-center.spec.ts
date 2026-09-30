import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

// Browser integration fixtures model the authority API. Actual file authority,
// proxy consumption and rejected physical requests are tested in pytest.
async function authority(page: Page, scenario: "network" | "plan" | "many" = "network") {
  let mode = "auto";
  let version = 0;
  let targetDecision: string | null = null;
  const host = scenario === "many" ? `${"research-".repeat(6)}data.${"department-".repeat(5)}example.com` : "research-data.department.example.com";
  const request = { approval_id: "approval-ui-1", run_id: "run-ui", task_id: "task-policy",
    fence_token: 1, kind: "network", capability: "tool.egress", target: { domain: host, port: 443 },
    target_fingerprint: "target-ui", status: "pending", version: 1, requested_at: Date.now() / 1000,
    expires_at: Date.now() / 1000 + 900, reason: "这是研究中新发现的数据来源，需要你确认是否允许访问。" };
  let pending = scenario === "plan" ? [] : scenario === "many" ? Array.from({ length: 6 }, (_, i) => ({ ...request, approval_id: `approval-${i}`, target: { ...request.target, domain: i === 0 ? host : `source-${i}.example.com` } })) : [request];
  let human: object | undefined = scenario === "plan" ? { action_id: "plan-ui", type: "plan_approval", allowed_actions: ["approve", "revise", "cancel"], payload: { content_markdown: "## 研究计划\n\n" + Array.from({ length: 20 }, (_, i) => `### ${i + 1}. 研究范围与验证方法\n\n围绕 **动力电池回收**，核对政策、市场数据与公开研究。\n\n`).join("") } } : undefined;
  const records: object[] = [];
  let failNext = false;
  await page.context().addCookies([{ name: "odr.access", value: "browser-fixture", domain: "127.0.0.1", path: "/" }]);
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname.includes("/events")) return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": connected\n\n" });
    let result: unknown = {};
    if (pathname.endsWith("/me")) result = { email: "researcher@example.com", roles: ["researcher"], permissions: [], display_name: "研究员" };
    else if (pathname.endsWith("/usage")) result = { accounting_status: "unavailable" };
    else if (pathname.endsWith("/team")) result = { enabled: false, members: [], tasks: [], messages: [] };
    else if (pathname.endsWith("/runs")) result = { items: [] };
    else if (pathname.endsWith("/run-ui")) result = { run_id: "run-ui", title: "电池回收行业研究", status: "running", last_event_id: 0, progress: { current_stage: "researching", task_items: {} }, pending_security_approvals: pending, pending_human_action: human };
    else if (pathname.endsWith("/egress-state")) result = { baseline_mode: "auto", run_setting: "profile", effective_mode: mode, can_resolve: true, can_interact: true, allowed_modes: ["manual", "auto"], health: { calls_used: 24, remaining_calls: 176 }, records, targets: [{ target_id: "target-ui", target: request.target, capability: "tool.egress", version, decision: targetDecision, updated_at: Date.now() / 1000, reason: "", policy_denied: false, classification: { verdict: "ask", source: "stage2", reason: "域名用途尚不明确。" } }] };
    else if (pathname.includes("/human-actions/")) { human = undefined; result = { status: "running" }; }
    else if (pathname.endsWith("/egress-mode")) { mode = route.request().postDataJSON().mode; result = { baseline_mode: "auto", effective_mode: mode }; }
    else if (pathname.endsWith("/decision")) { targetDecision = route.request().postDataJSON().decision; version++; result = { version, decision: targetDecision }; }
    else if (pathname.endsWith("/security-approvals")) result = { run_id: "run-ui", version, approvals: pending };
    else if (pathname.includes("/security-approvals/")) {
      if (failNext) { failNext = false; return route.fulfill({ status: 400, json: { detail: "invalid_request" } }); }
      const decision = route.request().postDataJSON().decision;
      const resolved = { ...request, decision, status: "resolved" };
      records.push(resolved); pending = []; result = resolved;
    }
    return route.fulfill({ status: 200, json: result });
  });
  return { fail: () => { failNext = true; }, requestAgain: () => { pending = [{ ...request, approval_id: "approval-ui-2" }]; } };
}

test("approval queue preserves failures, consumes one request, and restores state", async ({ page }) => {
  const fixture = await authority(page);
  await page.goto("/research/run-ui");
  await expect(page.getByText("1 项请求等待你的决定")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: /审批中心/ }).click();
  await expect(page.getByRole("button", { name: "允许此次访问", exact: true })).toBeEnabled();
  fixture.fail();
  await page.getByRole("button", { name: "允许此次访问", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("提交未完成");
  await page.getByRole("button", { name: "允许此次访问", exact: true }).click();
  await expect(page.getByText("暂时没有待处理请求")).toBeVisible();
  fixture.requestAgain();
  await expect(page.getByRole("button", { name: "允许此次访问", exact: true })).toBeEnabled();
  await page.getByRole("tab", { name: "域名权限" }).click();
  await page.getByText("复核或撤销权限").click();
  await page.getByRole("button", { name: "撤销并转人工" }).click();
  await expect(page.getByText("已撤销，转人工")).toBeVisible();
  await page.getByRole("button", { name: "逐项人工", exact: true }).click();
  await expect(page.getByText("当前：逐项人工")).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: /审批中心/ }).click();
  await page.getByRole("tab", { name: "域名权限" }).click();
  await expect(page.getByText("已撤销，转人工")).toBeVisible();
  await expect(page.getByText("当前：逐项人工")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /审批中心/ })).toBeFocused();
});

for (const theme of ["light", "dark"]) {
  test(`approval center visual review ${theme}`, async ({ page }, testInfo) => {
    await authority(page);
    await page.addInitScript((value) => localStorage.setItem("odr.interface-theme.v1", value), theme);
    await page.goto("/research/run-ui");
    await page.getByRole("button", { name: /审批中心/ }).click();
    await expect(page.getByRole("button", { name: "允许此次访问", exact: true })).toBeEnabled();
    await expect(page.getByRole("dialog")).toBeVisible();
    const overflows = await page.getByRole("dialog").evaluate((node) => node.scrollWidth > node.clientWidth);
    expect(overflows).toBe(false);
    const output = path.resolve("../output/playwright");
    await mkdir(output, { recursive: true });
    await page.screenshot({ path: path.join(output, `approval-center-${testInfo.project.name}-${theme}.png`), fullPage: false, animations: "disabled" });
  });
  for (const scenario of ["plan", "many"] as const) {
    test(`long ${scenario} layout ${theme}`, async ({ page }, testInfo) => {
      await authority(page, scenario);
      await page.addInitScript((value) => localStorage.setItem("odr.interface-theme.v1", value), theme);
      await page.goto("/research/run-ui");
      await page.getByRole("button", { name: /审批中心/ }).click();
      const primary = page.getByRole("button", { name: scenario === "plan" ? "批准并继续" : "允许此次访问", exact: true });
      await expect(primary).toBeEnabled();
      const dialog = page.getByRole("dialog");
      expect(await dialog.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(false);
      await expect(primary).toBeInViewport();
      const output = path.resolve("../output/playwright");
      await mkdir(output, { recursive: true });
      await page.screenshot({ path: path.join(output, `approval-center-${scenario}-${testInfo.project.name}-${theme}.png`), animations: "disabled" });
      if (scenario === "plan") {
        await primary.click();
        await expect(page.getByText("暂时没有待处理请求")).toBeVisible();
      } else {
        await page.getByRole("tab", { name: "域名权限" }).click();
        await expect(page.getByText("当前：自动分类")).toBeVisible();
        await page.screenshot({ path: path.join(output, `approval-permissions-${testInfo.project.name}-${theme}.png`), animations: "disabled" });
      }
    });
  }
}
