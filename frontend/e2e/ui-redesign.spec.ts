import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

// These fixtures exercise the real Next.js pages and existing HTTP contracts.
// They do not start a research model or mutate a user's account or documents.
const runId = "design-e2e";
const title = "企业 AI 搜索市场研究：产品能力、商业模式与竞争机会";
const artifactDir = path.resolve("../output/ui-redesign");

async function fixture(page: Page) {
  let complete = false;
  let feedbackFails = true;
  const feedbacks: unknown[] = [];
  const task = { task_id: "product", title: "产品能力与差异", status: "running", wave_id: "wave-1", source_count: 4, activity_label: "正在核验产品文档" };
  const document = { id: "document-1", filename: "产品功能与安全说明.pdf", status: "ready", media_type: "application/pdf", size_bytes: 248000, page_count: 8, chunk_count: 16, ocr_pages: 0, sha256: "abcdef123456", created_at: "2026-09-20T10:00:00Z", updated_at: "2026-09-22T09:00:00Z" };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const endpoint = url.pathname;
    let result: unknown = {};
    if (endpoint.endsWith("/events")) return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": connected\n\n" });
    if (endpoint.endsWith("/capabilities")) result = { editable_config_keys: ["allow_clarification", "research_model", "max_concurrent_research_units", "search_api"], defaults: { allow_clarification: true, research_model: "openai:gpt-test", max_concurrent_research_units: 5, search_api: "tavily" }, features: { document_research: { enabled: true, database: "ready" } }, config_schema: { properties: { allow_clarification: { type: "boolean", title: "开始前澄清问题" }, research_model: { type: "string", title: "研究模型" }, max_concurrent_research_units: { type: "integer", title: "并行任务上限", minimum: 1, maximum: 20 }, search_api: { type: "string", title: "搜索服务", enum: ["tavily", "none"] } } } };
    else if (endpoint.endsWith("/models")) result = { backend: "litellm", models: [{ name: "openai:gpt-test" }, { name: "anthropic:test-model" }] };
    else if (endpoint.endsWith("/runs")) result = route.request().method() === "POST" ? { run_id: runId } : { items: [{ run_id: runId, title, status: complete ? "completed" : "running" }] };
    else if (endpoint.endsWith(`/runs/${runId}`)) result = { run_id: runId, title, status: complete ? "completed" : "running", last_event_id: 10, progress: { current_stage: complete ? "finalizing" : "researching", task_items: { product: task }, sources: [{ source_id: "s1", title: "产品白皮书", domain: "example.com", url: "https://example.com/product", task_id: "product" }], latest_findings: [{ task_id: "product", summary: "已记录产品能力与访问控制方面的发现。" }] }, output: complete ? { markdown: "# 企业搜索研究\n\n## 核心判断\n\n企业方案需要核对资料权限。[来源](https://example.com/product)\n\n## 证据缺口\n\n定价仍需进一步核验。", publications: [] } : {} };
    else if (endpoint.endsWith("/usage")) result = { accounting_status: "unavailable", unavailable_reason: "no_usage_events", task_operations: { product: { model_call_count: 7, tool_call_count: 3 } } };
    else if (endpoint.endsWith("/activity")) result = { items: [], source: "summary_only", detail_level: "summary", last_event_id: 0, oldest_sequence: 0, has_more: false };
    else if (endpoint.endsWith("/security-approvals")) result = { approvals: [] };
    else if (endpoint.endsWith("/team")) result = { enabled: true, mode: "teams", status: "active", name: "研究团队", members: [{ member_id: "r1", name: "产品研究员", purpose: "核验产品能力", status: "active" }], tasks: [], messages: [], plans: [] };
    else if (endpoint.endsWith("/feedback")) {
      feedbacks.push(route.request().postDataJSON());
      if (feedbackFails) { feedbackFails = false; return route.fulfill({ status: 503, json: { detail: "稍后重试" } }); }
      result = { status: "accepted" };
    } else if (endpoint.endsWith("/documents")) result = { items: [document], total: 1 };
    else if (endpoint.endsWith("/documents/document-1")) result = document;
    else if (endpoint.endsWith("/chunks")) result = { items: [{ id: "chunk-1", document_id: "document-1", ordinal: 1, locator: "第 1 页", heading: "产品能力", text: "企业访问控制与来源引用。" }] };
    else if (endpoint.endsWith("/publications")) result = { items: [], capabilities: { supported_formats: ["pdf", "docx"] } };
    else if (endpoint.endsWith("/knowledge-bases")) result = { items: [{ id: "kb-ui", name: "企业研究资料" }] };
    else if (endpoint.endsWith("/health")) result = { generated_at: "2026-09-22T09:00:00Z", counts: { expired: 1, missing_topic: 1, pending_review: 2, sync_failed: 0, no_results: 0 }, companies: ["示例公司"], periods: ["2026"], groups: [{ company: "示例公司", period: "2026", expired: 1, missing_topic: 1, pending_review: 2, sync_failed: 0, no_results: 0 }], items: [{ kind: "missing_topic", company: "示例公司", period: "2026", title: "定价资料", source_id: "s1", url: "/documents/document-1", reason: "缺少有效资料" }], targets: [{ id: "target-1", company: "示例公司", period: "2026", topic: "定价", min_documents: 1, max_age_days: 90, available_documents: 0, missing_documents: 1 }], total: 1 };
    else if (endpoint.endsWith("/facts")) result = { items: [{ id: "fact-1", entity_name: "示例公司", metric: "访问控制", value_text: "支持企业单点登录", value_numeric: null, unit: "", scale: "", currency: "", data_period: "2026", status: "published", verification: "verified", adopted: true, evidence_count: 1 }] };
    else if (endpoint.endsWith("/facts/fact-1")) result = { evidence: [{ document_id: "document-1", generation_id: "generation-1", excerpt: "提供企业身份认证。" }], alternatives: [] };
    else if (/\/(pages|jobs)$/.test(endpoint)) result = { items: [] };
    else if (endpoint.endsWith("/admin/users")) result = [{ id: "user-1", email: "researcher@example.com", display_name: "研究员", status: "active", role_codes: ["researcher"], created_at: "2026-09-20T10:00:00Z" }];
    else if (endpoint.endsWith("/admin/roles")) result = [{ id: "role-1", code: "researcher", name: "研究员", description: "创建与管理研究", is_system: true, permission_codes: [] }];
    else if (endpoint.endsWith("/me")) result = { id: "user-1", email: "researcher@example.com", display_name: "研究员", roles: ["researcher"], permissions: [], status: "active" };
    else if (/\/(users|roles|permissions|audit|sessions)$/.test(endpoint)) result = [];
    return route.fulfill({ status: 200, json: result });
  });
  return { finish: () => { complete = true; }, feedbacks };
}

test.beforeEach(async () => { await mkdir(artifactDir, { recursive: true }); });

test("research flow retains URL state, feedback failures and report downloads", async ({ page }) => {
  const api = await fixture(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/research/new");
  await page.getByLabel("研究问题", { exact: true }).fill(title);
  await page.getByRole("button", { name: "研究偏好", exact: true }).click();
  await page.getByRole("radio", { name: /团队协作/ }).check();
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "启动研究" }).click();
  await expect(page).toHaveURL(new RegExp(`/research/${runId}`));
  await page.getByRole("tab", { name: "研究团队", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "用量", exact: true })).toBeFocused();
  await page.keyboard.press("Home");
  await expect(page.getByRole("tab", { name: "研究进展", exact: true })).toBeFocused();
  const task = page.locator('[data-task-id="product"]');
  await task.click();
  await expect(page).toHaveURL(/task=product/);
  await expect(page.getByRole("dialog")).toContainText("模型调用");
  await expect(page.getByRole("dialog")).toContainText("7");
  await page.getByRole("button", { name: "关闭任务详情" }).click();
  await expect(task).toBeFocused();
  await page.getByLabel("补充研究方向").fill("优先核验官方资料");
  await page.getByRole("button", { name: "发送补充" }).click();
  await expect(page.locator(".direction-composer").getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("补充研究方向")).toHaveValue("优先核验官方资料");
  await page.getByRole("button", { name: "发送补充" }).click();
  await expect(page.getByLabel("补充研究方向")).toHaveValue("");
  expect(api.feedbacks).toEqual([{ type: "direction", message: "优先核验官方资料" }, { type: "direction", message: "优先核验官方资料" }]);
  await page.getByRole("tab", { name: "研究团队", exact: true }).click();
  await page.getByRole("tab", { name: "消息", exact: true }).click();
  await expect(page.getByLabel("消息接收成员")).toBeVisible();
  api.finish();
  await page.goto(`/research/${runId}?view=report`);
  await expect(page.locator(".report-shell")).toContainText("证据缺口");
  await page.screenshot({ path: path.join(artifactDir, "report-1440.png"), fullPage: true });
  await page.getByRole("button", { name: "导出报告" }).click();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Markdown", exact: true }).click();
  expect((await download).suggestedFilename()).toContain(".md");
  await page.getByRole("dialog").getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "切换到深色模式" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: path.join(artifactDir, "report-dark-1440.png"), fullPage: true });
  expect(errors).toEqual([]);
});

test("settings uses keyboard-accessible categories and searchable models", async ({ page }) => {
  await fixture(page);
  await page.goto("/settings");
  await page.getByRole("button", { name: "模型与 Token 预算", exact: true }).click();
  await page.getByRole("button", { name: "研究模型", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索研究模型" }).fill("anthropic");
  await page.getByRole("button", { name: "anthropic:test-model", exact: true }).click();
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("已保存");
  await page.reload();
  await page.getByRole("button", { name: "模型与 Token 预算", exact: true }).click();
  await expect(page.getByRole("button", { name: "研究模型", exact: true })).toContainText("anthropic:test-model");
});

test("knowledge evidence and editing are available in dedicated panels", async ({ page }) => {
  await fixture(page);
  await page.goto("/knowledge/ledger");
  await page.getByRole("button", { name: "1 条 · 对比" }).click();
  await expect(page.getByRole("dialog")).toContainText("提供企业身份认证");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "添加事实", exact: true }).click();
  await expect(page.getByRole("dialog").getByLabel("企业 / 产品")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Wiki 档案" }).click();
  await expect(page.getByRole("radio", { name: "竞品档案", exact: true })).toBeChecked();
  await page.goto("/knowledge/health");
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await expect(page.getByRole("dialog").getByLabel("目标竞品")).toHaveValue("示例公司");
});

for (const width of [390, 768, 1024, 1440]) {
  test(`responsive pages have no horizontal overflow at ${width}px`, async ({ page }) => {
    await fixture(page);
    await page.setViewportSize({ width, height: 1000 });
    for (const [name, url, heading] of [
      ["run", `/research/${runId}`, title],
      ["new", "/research/new", "今天，想深入了解什么？"],
      ["settings", "/settings", "研究设置"],
      ["documents", "/documents", "让每份资料，都成为研究的依据"],
      ["knowledge", "/knowledge", "组织的知识，触手可及"],
      ["ledger", "/knowledge/ledger", "事实台账与知识 Wiki"],
      ["health", "/knowledge/health", "知识库健康与缺口"],
      ["account", "/account/security", "账户与会话"],
      ["admin", "/admin", "成员与权限"],
      ["login", "/login", "欢迎回来"],
    ]) {
      await page.goto(url);
      await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
      await expect(page.locator("body")).toHaveCSS("font-family", /IBM Plex Sans/);
      if (name === "run") await expect(page.locator('[data-task-id="product"]')).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `${name} overflows at ${width}`).toBe(true);
      await page.screenshot({ path: path.join(artifactDir, `${name}-${width}.png`), fullPage: true });
    }
    if (width === 390) {
      await page.goto(`/research/${runId}`);
      await page.getByRole("button", { name: "打开导航" }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("button", { name: "打开导航" })).toBeFocused();
      await page.getByRole("button", { name: "打开研究信息" }).click();
      await expect(page.getByRole("dialog")).toContainText("产品白皮书");
      await page.keyboard.press("Escape");
    }
  });
}
