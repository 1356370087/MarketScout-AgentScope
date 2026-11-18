import { expect, test, type Page } from "@playwright/test";

const actor = "11111111-1111-4111-8111-111111111111";
const workspace = "22222222-2222-4222-8222-222222222222";
const base = "33333333-3333-4333-8333-333333333333";
const planId = "44444444-4444-4444-8444-444444444444";
const itemId = "55555555-5555-4555-8555-555555555555";
const snapshotId = "66666666-6666-4666-8666-666666666666";
const oldSnapshotId = "77777777-7777-4777-8777-777777777777";

async function fixture(page: Page, quality = "passed") {
  const requests: { path: string; body: Record<string, unknown> }[] = [];
  const definition = { workspace_id: workspace, knowledge_base_id: base, name: "竞品定价与功能跟踪", objective: "持续核实竞品套餐价格、使用条件和新功能变化。", dimensions: ["价格", "套餐条件"], sources: [{ kind: "web", url: "https://example.com/pricing", document_id: null, label: "官方定价页" }], budget: { run_micro_usd: 1000000, month_micro_usd: 5000000, assessment_micro_usd: 100000 }, cadence: "daily", timezone: "Asia/Shanghai", mode: "observe", profile_revision_id: null, configurable: {}, reviewer_ids: [] };
  const plan = { capabilities: { manage: true, review: true, authorize: true }, id: planId, name: definition.name, objective: definition.objective, workspace_id: workspace, knowledge_base_id: base, owner_id: actor, version: 1, status: "draft", mode: "observe", definition, baseline: {}, sources: [{ id: "source", definition: definition.sources[0], sequence: 2, last_checked_at: "2026-11-08T00:00:00Z", next_run_at: "2026-11-09T00:00:00Z", error: null }], budget_periods: [{ period: "2026-11", reserved: 100000, settled: 20000 }] };
  const snapshot = { id: snapshotId, metadata: { title: "官方定价页", url: "https://example.com/pricing", published_at: null }, observed_at: "2026-11-08T00:00:00Z", content: "The new package costs USD 65 per user per month. Existing contracts retain their original rates.", next_offset: null };
  const oldSnapshot = { ...snapshot, id: oldSnapshotId, observed_at: "2026-11-01T00:00:00Z", content: "The former package costs USD 60 per user per month." };
  const item = { capabilities: { manage: true, review: true, authorize: true }, id: itemId, plan_id: planId, owner_id: actor, version: 3, question: "核实套餐价格是否变化，以及对现有客户的影响", status: "awaiting_review", quality_status: quality, reason: "发现价格及生效条件变化，需要核实。", run_id: "research-fixture", create_time: "2026-11-08T00:01:00Z", manifest: { snapshots: [snapshot, oldSnapshot], events: [{ id: "change-event", kind: "content_changed", previous_snapshot_id: oldSnapshotId, snapshot_id: snapshotId, create_time: "2026-11-08T00:00:00Z" }] }, result: { report_review: { decision: quality === "passed" ? "pass" : "fail", status: quality === "passed" ? "completed" : "failed", attempt: 1, issue_count: quality === "passed" ? 0 : 2 }, report: "## 变化简报\n新套餐为每用户每月 **65 美元**。现有合同保留原价格。\n\n正式发布前请核验适用条件。", evidence: [{ evidence_id: "price", claim: "新套餐每用户每月 65 美元", supporting_excerpt: snapshot.content, source_url: snapshot.metadata.url, observation_snapshot_id: snapshotId }] }, review: {} };
  await page.context().addCookies([{ name: "odr.access", value: "mock-session", domain: "127.0.0.1", path: "/" }, { name: "odr.csrf", value: "mock-csrf", domain: "127.0.0.1", path: "/" }]);
  await page.route("**/api/**", async route => {
    const req = route.request(), path = new URL(req.url()).pathname;
    const body = req.method() === "GET" ? undefined : req.postDataJSON() as Record<string, unknown>;
    if (body) requests.push({ path, body });
    let data: unknown;
    if (path.endsWith("/me")) data = { id: actor, display_name: "验收研究员", email: "researcher@example.invalid", roles: ["researcher"], permissions: ["research:run:create"] };
    else if (path.endsWith("/capabilities")) data = { runtime_ready: true, configuration_required: false };
    else if (path.endsWith("/runs") && !path.includes("/intelligence/")) data = { items: [] };
    else if (path.endsWith("/intelligence/options")) data = { bases: [{ id: base, name: "市场知识库", workspace_id: workspace, workspace_name: "研究团队", can_submit: true, can_review: true }], documents: [], facts: [{ id: snapshotId, name: "原套餐每月 60 美元", knowledge_base_id: base }], wiki_revisions: [] };
    else if (path.endsWith("/intelligence/plans") && req.method() === "POST") data = { ...plan, ...body };
    else if (path.endsWith("/intelligence/plans")) data = { items: [plan] };
    else if (path.endsWith(`/plans/${planId}/activate`)) { expect(body?.expected_version).toBe(plan.version); expect(body?.command_id).toBeTruthy(); plan.version++; plan.status = "active"; plan.mode = String(body?.mode); data = plan; }
    else if (path.endsWith(`/plans/${planId}/baseline`)) { expect(body?.expected_version).toBe(plan.version); plan.version++; plan.baseline = { fact_ids: body?.fact_ids, wiki_revision_ids: body?.wiki_revision_ids }; data = plan; }
    else if (path.endsWith(`/plans/${planId}/requests`)) { expect(body?.expected_version).toBe(plan.version); data = item; }
    else if (path.endsWith(`/plans/${planId}/authorizations`)) data = { items: [] };
    else if (path.endsWith(`/plans/${planId}/decisions`)) data = { items: [{ id: "decision", would_start: true, assessment: { decision: "research", reason: "定价正文发生实质变化，需核实适用套餐与生效条件。" }, create_time: "2026-11-08T00:00:00Z" }] };
    else if (path.endsWith(`/plans/${planId}`)) data = plan;
    else if (path.endsWith(`/items/${itemId}/review`)) { expect(body?.expected_version).toBe(3); data = { status: "awaiting_review", version: 4 }; }
    else if (path.endsWith(`/items/${itemId}/runs`)) data = { items: [{ run_id: "prior-fixture", create_time: "2026-11-01T00:00:00Z", result: { summary: "历史摘要", quality_status: "partial" } }] };
    else if (path.endsWith(`/items/${itemId}/runs/prior-fixture`)) data = { run_id: "prior-fixture", result: { report: "历史完整报告：原合同条款仍然有效。" } };
    else if (path.endsWith(`/items/${itemId}`)) data = item;
    else if (path.endsWith("/intelligence/items")) data = { items: [item] };
    else if (path.endsWith(`/snapshots/${snapshotId}`)) data = snapshot;
    else if (path.endsWith(`/snapshots/${oldSnapshotId}`)) data = oldSnapshot;
    else if (path.endsWith("/intelligence/inbox")) data = { items: [{ id: "notification", kind: "review_ready", item_id: itemId, plan_id: planId, read_at: null, delivery_status: "sent", create_time: "2026-11-08T00:01:00Z" }] };
    else if (path.endsWith("/integrations/feishu")) data = { bound: true, enabled: true };
    else if (path.endsWith("/integrations/feishu/callback")) data = { bound: true, enabled: false };
    else { await route.fulfill({ status: 404, json: { detail: "unexpected_mock_route", path } }); return; }
    await route.fulfill({ json: data });
  });
  return requests;
}

test("continuous intelligence workspace and explicit authorization", async ({ page }, info) => {
  const requests = await fixture(page);
  await page.goto("/intelligence");
  await expect(page.getByRole("heading", { name: "持续情报", exact: true })).toBeVisible();
  await expect(page.getByText("竞品定价与功能跟踪", { exact: true })).toBeVisible();
  await page.getByText("竞品定价与功能跟踪", { exact: true }).click();
  await page.getByRole("button", { name: "启用或更新授权" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText(/计划月度上限/)).toBeVisible();
  await dialog.getByRole("combobox").selectOption("auto");
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "确认授权并启用" }).click();
  await expect(page.getByText("已启用", { exact: true })).toBeVisible();
  expect(requests.some(r => r.body.mode === "auto" && typeof r.body.expires_at === "string")).toBeTruthy();
  await page.screenshot({ path: info.outputPath("plan.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2)).toBeTruthy();
});

test("quality and evidence remain visible before publication", async ({ page }, info) => {
  const requests = await fixture(page);
  await page.goto(`/intelligence/items/${itemId}`);
  await expect(page.getByText("质量验收通过", { exact: true })).toBeVisible();
  await page.getByText("新套餐每用户每月 65 美元", { exact: true }).click();
  await page.getByRole("button", { name: "核对原始快照" }).click();
  await expect(page.locator(".intelligence-source")).toContainText("Existing contracts retain their original rates.");
  await page.getByRole("button", { name: "1. 准备资料审阅" }).click();
  expect(requests.some(r => r.body.action === "prepare_sources")).toBeTruthy();
  await expect(page.getByRole("button", { name: "3. 确认已发布并更新基线" })).toBeDisabled();
  await page.screenshot({ path: info.outputPath("review.png"), fullPage: true });
});

test("partial research cannot be promoted to knowledge", async ({ page }) => {
  await fixture(page, "partial");
  await page.goto(`/intelligence/items/${itemId}`);
  await expect(page.getByText("部分成果，尚未通过验收", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "1. 准备资料审阅" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "2. 生成知识草稿" })).toBeDisabled();
});

test("Feishu callback binds once and returns to normal controls", async ({ page }) => {
  const requests = await fixture(page);
  await page.goto("/intelligence/feishu/callback?code=mock-code&state=mock-state");
  await expect(page.getByText("飞书账号已绑定。请返回持续情报页面开启通知。")).toBeVisible();
  expect(requests.filter(r => r.path.endsWith("/integrations/feishu/callback"))).toHaveLength(1);
  await expect(page).toHaveURL(/\/intelligence\/feishu\/callback$/);
});

test("published baseline can be linked before an explicit observe-mode review", async ({ page }) => {
  const requests = await fixture(page);
  await page.goto(`/intelligence/plans/${planId}`);
  await page.getByRole("button", { name: "关联已有结论" }).click();
  await page.getByRole("checkbox", { name: "原套餐每月 60 美元" }).check();
  await page.getByRole("button", { name: "保存已发布基线" }).click();
  await expect(page.getByText("已关联 1 条事实、0 篇 Wiki。")).toBeVisible();
  await page.getByRole("button", { name: "启用或更新授权" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "确认授权并启用" }).click();
  await page.getByRole("button", { name: "发起人工复核" }).click();
  await page.getByLabel("需要核实的问题").fill("核实新价格是否适用于已有合同");
  await page.getByRole("button", { name: "创建复核研究" }).click();
  await expect(page).toHaveURL(new RegExp(`/intelligence/items/${itemId}$`));
  expect(requests.some(r => r.path.endsWith("/requests") && r.body.question === "核实新价格是否适用于已有合同")).toBeTruthy();
});

test("history expands through the authorized work item", async ({ page }) => {
  await fixture(page);
  await page.goto(`/intelligence/items/${itemId}`);
  await expect(page.getByText("历史完整报告：原合同条款仍然有效。")).toHaveCount(0);
  await page.locator("summary").filter({ hasText: "部分成果，尚未通过验收" }).click();
  await expect(page.getByText("历史完整报告：原合同条款仍然有效。")).toBeVisible();
});

test("authorization confirmation does not silently adopt a newer plan version", async ({ page }) => {
  await fixture(page);
  await page.goto(`/intelligence/plans/${planId}`);
  await page.getByRole("button", { name: "启用或更新授权" }).click();
  await page.getByRole("dialog").getByRole("checkbox").check();
  await page.evaluate(async id => { await fetch(`/api/research/intelligence/plans/${id}/activate`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ expected_version: 1, command_id: "concurrent-editor", mode: "observe" }) }); }, planId);
  await expect(page.getByText("计划已更新，请关闭此窗口并重新检查授权范围。")).toBeVisible({ timeout: 8000 });
  await expect(page.getByRole("button", { name: "确认授权并启用" })).toBeDisabled();
});


test("triggering evidence shows the original and changed versions", async ({ page }) => {
  await fixture(page);
  await page.goto(`/intelligence/items/${itemId}`);
  await page.locator("summary").filter({ hasText: "新旧观察对比" }).click();
  await expect(page.locator(".intelligence-source").filter({ hasText: "USD 60" })).toBeVisible();
  await expect(page.locator(".intelligence-source").filter({ hasText: "USD 65" })).toBeVisible();
});
