import { mkdir } from "node:fs/promises";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

async function knowledgeFixture(page: Page, status = "ready") {
  const searches: Record<string, unknown>[] = [];
  const runs: Record<string, unknown>[] = [];
  const chunkQueries: URLSearchParams[] = [];
  const doc = { id: "doc-ready", filename: "定价资料.pdf", status, current_generation_id: "generation-new",
    media_type: "application/pdf", size_bytes: 24000, page_count: 100, chunk_count: 240, ocr_pages: 0,
    sha256: "fixture", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" };
  const chunk = { id: "segment-205", document_id: doc.id, generation_id: "generation-old", generation_status: "published",
    version_no: 1, ordinal: 205, locator: "工作表 Pricing，第 206 行", text: "历史版本的年度价格为 100 美元。" };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const endpoint = url.pathname;
    let body: unknown = {};
    if (endpoint.endsWith("/me")) body = { id: "user", email: "fixture@example.com", roles: ["researcher"], permissions: [], status: "active" };
    else if (endpoint.endsWith("/capabilities")) body = { defaults: { research_model: "fixture" }, editable_config_keys: [], features: { document_research: { enabled: true, database: "ready" } } };
    else if (endpoint.endsWith("/knowledge-bases")) body = { items: [{ id: "team-base", name: "团队产品库" }] };
    else if (endpoint.endsWith("/collections")) body = { items: [{ id: "pricing-collection", name: "定价集合" }] };
    else if (endpoint.endsWith("/search-profiles")) body = { items: [{ version: "compare-v1", note: "对比型检索", is_default: true }], index_profile: { model: "embedding-v1", dimensions: 1536, revision: "v1" } };
    else if (endpoint.endsWith("/knowledge/search")) {
      searches.push(route.request().postDataJSON());
      body = { query_id: "query", rerank_completed: true, documents: [{ document_id: doc.id, filename: doc.filename, generation_id: "generation-old" }],
        results: [{ segment_id: chunk.id, document_id: doc.id, generation_id: chunk.generation_id, filename: doc.filename,
          text: chunk.text, score: .03, relevance: 3, source_uri: `/documents/${doc.id}?chunk=${chunk.id}` }] };
    } else if (endpoint.endsWith("/documents")) body = { items: [doc], total: 1 };
    else if (endpoint.endsWith(`/documents/${doc.id}`)) body = doc;
    else if (endpoint.endsWith(`/chunks/${chunk.id}`)) body = chunk;
    else if (endpoint.endsWith("/chunks")) { chunkQueries.push(url.searchParams); body = { items: [chunk] }; }
    else if (endpoint.endsWith("/runs")) {
      if (route.request().method() === "POST") { runs.push(route.request().postDataJSON()); body = { run_id: "knowledge-ui" }; }
      else body = { items: [] };
    } else if (endpoint.endsWith("/runs/knowledge-ui")) body = { run_id: "knowledge-ui", title: "定价研究", status: "completed", last_event_id: 0, progress: { task_items: {}, sources: [] }, output: {} };
    else if (endpoint.endsWith("/usage")) body = { accounting_status: "unavailable", unavailable_reason: "no_usage_events" };
    else if (endpoint.endsWith("/events")) return route.fulfill({ contentType: "text/event-stream", body: ": connected\n\n" });
    await route.fulfill({ json: body });
  });
  return { searches, runs, chunkQueries };
}

test("knowledge scope and historical choices reach research creation", async ({ page }, testInfo) => {
  const fixture = await knowledgeFixture(page);
  await page.goto("/knowledge?kb_id=team-base&query=比较年度定价");
  if (testInfo.project.name === "mobile") await page.getByRole("button", { name: "切换到深色模式" }).click();
  await page.getByRole("button", { name: "范围与筛选" }).click();
  await page.getByRole("button", { name: "选择 团队产品库 的集合" }).click();
  await page.getByRole("checkbox", { name: "定价集合", exact: true }).check();
  await page.getByText("版本与检索设置", { exact: true }).click();
  await page.getByRole("radio", { name: "历史发布时点", exact: true }).check();
  await page.getByLabel("发布时间截止", { exact: true }).fill("2026-01-02");
  await page.getByLabel("业务有效日期", { exact: true }).fill("2025-12-31");
  await page.getByRole("button", { name: "检索配置", exact: true }).click();
  await page.getByRole("button", { name: "对比型检索", exact: true }).click();
  await mkdir(path.resolve("../output/knowledge-plan-a"), { recursive: true });
  await page.screenshot({ path: path.resolve(`../output/knowledge-plan-a/scope-${testInfo.project.name}.png`), animations: "disabled" });
  await page.getByRole("dialog").getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "检索", exact: true }).click();
  await expect(page.getByRole("link", { name: "用这个问题继续研究" })).toBeVisible();
  await page.getByLabel("检索 / 提问内容").fill("尚未提交的新问题");
  await page.getByRole("link", { name: "用这个问题继续研究" }).click();
  await expect(page.getByLabel("研究问题", { exact: true })).toHaveValue("比较年度定价");
  await expect(page.getByRole("button", { name: "企业资料", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "启动研究", exact: true }).click();
  await expect(page).toHaveURL(/research\/knowledge-ui/);
  expect(fixture.searches[0]).toMatchObject({ kb_ids: [], collection_ids: ["pricing-collection"], version_mode: "as_of", as_of_published: "2026-01-02" });
  expect(fixture.runs[0]).toMatchObject({ source_selection: { mode: "documents", sources: [{ type: "collection", id: "pricing-collection" }],
    retrieval: { version_mode: "as_of", as_of_published: "2026-01-02", as_of_valid: "2025-12-31", profile_version: "compare-v1" } } });
});

test("a citation past the first 200 segments opens its original generation", async ({ page }, testInfo) => {
  const fixture = await knowledgeFixture(page);
  await page.goto("/documents/doc-ready?chunk=segment-205");
  await expect(page.locator("#chunk-segment-205")).toContainText("历史版本的年度价格为 100 美元。");
  await expect(page.locator("#chunk-segment-205")).toContainText("版本 1");
  await expect(page.locator("#chunk-segment-205")).toBeFocused();
  expect(fixture.chunkQueries.at(-1)?.get("generation_id")).toBe("generation-old");
  expect(fixture.chunkQueries.at(-1)?.get("offset")).toBe("200");
  await mkdir(path.resolve("../output/knowledge-plan-a"), { recursive: true });
  await page.screenshot({ path: path.resolve(`../output/knowledge-plan-a/citation-${testInfo.project.name}.png`), fullPage: true, animations: "disabled" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("published material remains readable while a new version is processing", async ({ page }) => {
  await knowledgeFixture(page, "processing");
  await page.goto("/documents/doc-ready");
  await expect(page.getByRole("region", { name: "文档正文" })).toContainText("历史版本的年度价格为 100 美元。");
  await expect(page.getByRole("link", { name: "用于新研究", exact: true })).toBeVisible();
});
