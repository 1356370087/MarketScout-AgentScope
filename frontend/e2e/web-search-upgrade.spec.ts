import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

const runId = "web-upgrade-e2e";
const artifactDir = path.resolve("../output/playwright/web-upgrade");
const configurable = {
  search_api: "tavily", search_providers: null, search_max_concurrency: 4,
  openai_search_model: null, anthropic_search_model: null, web_pipeline_mode: "enforced",
  web_pipeline_shadow_sample_rate: 0.1, web_shadow_fetch_top_k: 2, web_shadow_timeout_seconds: 60,
};
const properties = {
  search_api: { type: "string", enum: ["tavily", "openai", "anthropic", "bing", "brave", "none"] },
  search_providers: { anyOf: [{ type: "array" }, { type: "null" }] },
  search_max_concurrency: { type: "integer", minimum: 1, maximum: 16 },
  openai_search_model: { anyOf: [{ type: "string" }, { type: "null" }] },
  anthropic_search_model: { anyOf: [{ type: "string" }, { type: "null" }] },
  web_pipeline_mode: { type: "string", enum: ["legacy", "shadow", "enforced"] },
  web_pipeline_shadow_sample_rate: { type: "number", minimum: 0, maximum: 1 },
  web_shadow_fetch_top_k: { type: "integer", minimum: 1, maximum: 8 },
  web_shadow_timeout_seconds: { type: "number", minimum: 1, maximum: 300 },
};

async function fixture(page: Page) {
  const requests: Array<{ configurable: Record<string, unknown> }> = [];
  const task = { task_id: "web", title: "检索 Python 官方文档", status: "running", activity_available: true, activity_event_count: 4 };
  const events = [
    { sequence: 1, type: "tool.started", title: "并行搜索", summary: "Python 官方文档", payload: { tool_call_id: "web-call", tool_name: "web_search" } },
    { sequence: 2, type: "tool.progress", title: "收到搜索结果", summary: "Python 官方文档", payload: { tool_call_id: "web-call", tool_name: "web_search", provider: "bing", result_count: 5, web_phase: "query_completed" } },
    { sequence: 3, type: "tool.progress", title: "搜索渠道暂不可用", summary: "rate_limited", payload: { tool_call_id: "web-call", tool_name: "web_search", provider: "brave", error_code: "rate_limited", web_phase: "provider_failed" } },
    { sequence: 4, type: "tool.completed", title: "搜索完成", summary: "保留 Bing 返回的候选来源", payload: { tool_call_id: "web-call", tool_name: "web_search", source_count: 5 } },
  ].map((item) => ({ schema_version: 1, event_id: `web-${item.sequence}`, run_id: runId, task_id: "web",
    timestamp: "2026-10-04T00:00:00Z", kind: "tool", phase: "tool_execution", status: item.sequence === 3 ? "warning" : item.sequence === 4 ? "success" : "running", ...item }));
  await page.route("**/api/**", async (route) => {
    const endpoint = new URL(route.request().url()).pathname;
    let result: unknown = {};
    if (endpoint.endsWith("/events")) return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": connected\n\n" });
    if (endpoint.endsWith("/capabilities")) result = { editable_config_keys: Object.keys(configurable), defaults: configurable, config_schema: { properties }, features: { memory: false } };
    else if (endpoint.endsWith("/models")) result = { backend: "litellm", models: [{ name: "if-openai-search-v1" }, { name: "if-anthropic-search-v1" }] };
    else if (endpoint.endsWith("/runs")) {
      if (route.request().method() === "POST") { requests.push(route.request().postDataJSON()); result = { run_id: runId }; }
      else result = { items: [] };
    } else if (endpoint.endsWith(`/runs/${runId}`)) result = { run_id: runId, title: "Python 官方文档", status: "running", last_event_id: 0,
      progress: { current_stage: "researching", task_items: { web: task }, sources: [] }, output: {} };
    else if (endpoint.endsWith("/activity")) result = { items: events, source: "native", detail_level: "summary", last_event_id: 4, oldest_sequence: 1, has_more: false };
    else if (endpoint.endsWith("/usage")) result = { accounting_status: "unavailable", unavailable_reason: "fixture", task_operations: {} };
    else if (endpoint.endsWith("/me")) result = { user_id: "fixture", email: "fixture@example.com", display_name: "验证用户", roles: ["researcher"], permissions: [] };
    else if (endpoint.endsWith("/documents")) result = { items: [] };
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(result) });
  });
  return requests;
}

test("parallel providers persist and are sent when creating a research run", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const requests = await fixture(page);
  await page.goto("/settings");
  await page.getByRole("button", { name: "来源与证据", exact: true }).click();
  await expect(page.getByRole("checkbox", { name: "Tavily" })).toBeChecked();
  await page.getByRole("checkbox", { name: "Bing" }).check();
  await page.getByRole("checkbox", { name: "Brave" }).check();
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("已保存");
  await page.reload();
  await page.getByRole("button", { name: "来源与证据", exact: true }).click();
  await expect(page.getByRole("checkbox", { name: "Bing" })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Brave" })).toBeChecked();
  await mkdir(artifactDir, { recursive: true });
  await page.screenshot({ path: path.join(artifactDir, `settings-${testInfo.project.name}.png`), fullPage: true });
  await page.goto("/research/new");
  await page.getByRole("button", { name: "公开网络", exact: true }).click();
  await page.getByLabel("研究问题", { exact: true }).fill("检索 Python 官方文档，核对上下文管理器语法。");
  await page.getByRole("button", { name: "启动研究", exact: true }).click();
  await expect.poll(() => requests.length).toBe(1);
  expect(requests[0].configurable.search_providers).toEqual(["tavily", "bing", "brave"]);
  expect(errors).toEqual([]);
});

test("one tool card displays progress and a partial provider failure", async ({ page }, testInfo) => {
  await fixture(page);
  await page.goto(`/research/${runId}`);
  await page.getByRole("button", { name: /检索 Python 官方文档/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("list", { name: "搜索与抓取进度" })).toBeVisible();
  await expect(dialog.getByText("bing · 收到搜索结果")).toBeVisible();
  await expect(dialog.getByText("brave · 搜索渠道暂不可用")).toBeVisible();
  await expect(dialog.getByText("5 个候选来源")).toBeVisible();
  await expect(dialog.locator(".web-progress-list")).toHaveCount(1);
  await mkdir(artifactDir, { recursive: true });
  await page.screenshot({ path: path.join(artifactDir, `progress-${testInfo.project.name}.png`) });
});
