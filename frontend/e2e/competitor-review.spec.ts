import { createHash, randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { authenticatedRequest } from "./authenticated-request";

const path = process.env.E2E_COMPETITOR_PATH;
const skills = ["source_verification", "comparative_research", "evidence_synthesis"];
const domains = ["dify.ai", "fastgpt.io", "fastgpt.cn"];
const sha = (body: string | Buffer) => createHash("sha256").update(body).digest("hex");
test.skip(!["fresh", "derived", "decline", "refuse", "preview"].includes(path ?? ""), "显式选择独立竞品验收场景");

test("competitor review through the native application", async ({ page }, info) => {
  test.setTimeout(path === "preview" ? 90_000 : 3_600_000);
  page.setDefaultTimeout(20_000);
  await page.goto("/login");
  await page.getByLabel("工作邮箱").fill(process.env.E2E_ADMIN_EMAIL!);
  await page.getByLabel("密码", { exact: true }).fill(process.env.E2E_ADMIN_PASSWORD!);
  await page.getByRole("button", { name: "进入研究台" }).click();
  await expect(page).toHaveURL(/\/(research\/new|admin\/configurations)$/);
  const api = authenticatedRequest(page);
  const existingChild = process.env.E2E_COMPETITOR_EXISTING_CHILD;
  let runId = existingChild || process.env.E2E_COMPETITOR_PARENT!;
  if (path === "fresh" && !existingChild) {
    const cap = Number(process.env.E2E_AUTHORIZED_RUN_MICRO_USD);
    expect(Number.isSafeInteger(cap) && cap > 0).toBe(true);
    const entrypoints = (process.env.E2E_COMPETITOR_ENTRY_URLS ?? "").split(/\s+/).filter(Boolean);
    for (const entry of entrypoints) {
      const url = new URL(entry);
      expect(url.protocol).toBe("https:");
      expect(domains.some(domain => url.hostname === domain || url.hostname.endsWith("." + domain))).toBe(true);
    }
    const fixedPages = process.env.E2E_COMPETITOR_EXACT_URLS === "true";
    if (fixedPages) expect(entrypoints.length).toBeGreaterThan(0);
    const leads = entrypoints.length ? (fixedPages
      ? "\n只从本次选定的官方原文取证，引用本轮实际读取的页面。核价日以该定价页成功读取的时间为准。\n"
      : "\n上一次读取的官网目录列出这些资料入口，请直接读取原文再判断是否支持相应事实。\n") + entrypoints.join("\n") : "";
    const created = await api.post("/api/research/runs", { headers: { "Idempotency-Key": process.env.E2E_COMPETITOR_CREATE_KEY || randomUUID() }, data: {
      title: "Dify 与 FastGPT 云套餐竞品验证",
      messages: [{ role: "user", content: "只用一个主研究任务，依次读取 source_verification、comparative_research、evidence_synthesis 三项 Skills 后再读取网页。在所选官网内比较 Dify 和 FastGPT 的 RAG 功能、部署方式及入门付费云套餐。用同一口径列明币种、计费单位、实际月付和年付金额及核价日期。每项事实就近引用直接原文，不能证实的列为缺口。正文不超过 1200 字，并提供一张对比表。最多读取 12 页。" + leads }],
      source_selection: { mode: "specific", sources: fixedPages ? entrypoints.map(url => ({ type: "url", url })) : domains.map(domain => ({ type: "domain", domain })) },
      configurable: { max_run_cost_micro_usd: cap, max_fetches_per_run: 12, enable_human_in_loop: true,
        ...(fixedPages && process.env.E2E_COMPETITOR_BOUNDED_REVIEW === "true"
          ? { report_review_max_revisions: 1, final_report_model_max_tokens: 4800 } : {}) },
    } });
    expect(created.ok(), await created.text()).toBe(true);
    runId = (await created.json()).run_id;
  }
  expect(runId).toBeTruthy();
  await page.goto(`/research/${runId}`);
  if (!existingChild && (path === "derived" || path === "refuse" || path === "preview")) {
    const parent = await (await api.get(`/api/research/runs/${runId}`)).json();
    const original = process.env.E2E_COMPETITOR_ORIGINAL_PARENT;
    if (original) {
      expect(parent.status === "failed" || parent.output.completion_status === "partial").toBe(true);
      const visited = new Set<string>();
      let ancestor = runId;
      while (ancestor !== original) {
        expect(visited.has(ancestor)).toBe(false);
        visited.add(ancestor);
        const prior = await (await api.get(`/api/research/runs/${ancestor}/review-versions`)).json();
        expect(prior.parent?.parent_run_id).toBeTruthy();
        ancestor = prior.parent.parent_run_id;
      }
    } else {
      expect(parent.output.completion_status).toBe("partial");
    }
    if (parent.output.markdown) await page.getByRole("tab", { name: "报告", exact: true }).click();
    await page.getByText("基于归档草稿重新复核", { exact: true }).click();
    if (process.env.E2E_COMPETITOR_BASE_SHA) await page.getByRole("combobox", { name: "基础草稿", exact: true }).selectOption(process.env.E2E_COMPETITOR_BASE_SHA);
    if (process.env.E2E_SUPPLEMENT_URLS) {
      await page.getByLabel(/补充来源网址/).fill(process.env.E2E_SUPPLEMENT_URLS);
      const ids = (process.env.E2E_SUPPLEMENT_REQUIREMENTS ?? "").split(",").filter(Boolean);
      const versions = await (await api.get(`/api/research/runs/${runId}/review-versions`)).json();
      for (const id of ids) {
        const goal = versions.requirements.find((item: { id: string }) => item.id === id);
        expect(goal).toBeDefined();
        const choice = page.getByRole("checkbox", { name: goal.subject ? `${goal.subject} · ${goal.text}` : goal.text, exact: true });
        await expect(choice).toHaveValue(id);
        await choice.check();
      }
    }
    const cap = path === "preview" ? 0.2 : path === "refuse" ? 0.000001 : Number(process.env.E2E_AUTHORIZED_RUN_MICRO_USD) / 1_000_000;
    expect(cap).toBeGreaterThan(0);
    await page.getByLabel(/本次复核费用上限/).fill(String(cap));
    await page.getByRole("button", { name: "查看复核费用" }).click();
    await expect(page.getByRole("button", { name: "确认费用并创建复核" })).toBeEnabled();
    if (path === "preview") {
      const versions = await (await api.get(`/api/research/runs/${runId}/review-versions`)).json();
      for (const child of versions.children) {
        if (child.completion_status === "partial") {
          await expect(page.locator(`section[aria-label="复核版本与执行回执"] a[href="/research/${child.run_id}"]`)).toHaveText(/部分交付/);
        }
      }
      const selected = await page.getByRole("combobox", { name: "基础草稿", exact: true }).inputValue();
      await page.getByRole("button", { name: "查看归档原文" }).click();
      const archived = page.locator('section[aria-label="复核版本与执行回执"] pre');
      await expect.poll(async () => sha(await archived.textContent() ?? "")).toBe(selected);
      await page.getByRole("button", { name: "收起原文" }).click();
      const screenshot = info.outputPath("draft-review-preview.png");
      await page.screenshot({ path: screenshot });
      await info.attach("review-preview", { path: screenshot, contentType: "image/png" });
      return;
    }
    await page.getByRole("button", { name: "确认费用并创建复核" }).click();
    const link = page.getByRole("link", { name: "打开复核任务" });
    const href = await link.getAttribute("href");
    expect(href).toMatch(/^\/research\/[a-f0-9]{32}$/);
    await link.click();
    await expect(page).toHaveURL(new RegExp(href + "$"));
    runId = href!.split("/").pop()!;
    expect(runId).not.toBe(process.env.E2E_COMPETITOR_PARENT);
  }
  console.log("竞品运行", runId);
  if (path === "refuse") {
    const version = await (await api.get(`/api/research/runs/${runId}/review-versions`)).json();
    expect(version.parent?.parent_run_id).toBe(process.env.E2E_COMPETITOR_PARENT);
    expect(version.parent.max_cost_micro_usd).toBe(1);
    const budget = () => api.get(`/api/research/runs/${runId}/budget`).then(r => r.json());
    await expect.poll(async () => (await (await api.get(`/api/research/runs/${runId}`)).json()).pending_human_action?.type,
      { timeout: 180000 }).toBe("budget_approval");
    const before = await budget();
    expect(before.limits.cost_micro_usd).toBe(1);
    expect(before.used.cost_micro_usd ?? 0).toBe(0);
    if (!await page.getByRole("dialog").isVisible()) await page.getByRole("button", { name: /审批中心/ }).click();
    await page.getByRole("button", { name: "不增加，生成部分报告" }).click();
  }
  await expect.poll(async () => {
    const result = await (await api.get(`/api/research/runs/${runId}`)).json();
    if (["failed", "cancelled"].includes(result.status)) throw new Error("竞品验证提前结束 " + result.status);
    if ((result.pending_human_action || result.pending_security_approvals?.length) && !await page.getByRole("dialog").isVisible()) {
      await page.getByRole("button", { name: /审批中心/ }).click();
    }
    return result.status;
  }, { timeout: 3_480_000, intervals: [2000, 5000, 10000] }).toBe("completed");
  const final = (await (await api.get(`/api/research/runs/${runId}`)).json()).output;
  if (path === "decline" || path === "refuse") {
    expect(final.completion_status).toBe("partial");
    expect(final.report_review?.skipped).toBe(true);
    expect(final.stop_reason).toBe("report_budget_extension_denied");
    if (path === "refuse") {
      const after = await (await api.get(`/api/research/runs/${runId}/budget`)).json();
      expect(after.used.cost_micro_usd ?? 0).toBe(0);
      expect(after.reserved.cost_micro_usd ?? 0).toBe(0);
    }
  } else {
    expect(final.completion_status).toBe("success");
    expect(final.report_review?.decision).toBe("pass");
    expect(final.report_review?.skipped || final.report_review?.degraded).toBeFalsy();
    expect(final.report_review.draft_sha256).toBe(sha(final.markdown));
  }
  const versions = await (await api.get(`/api/research/runs/${runId}/review-versions`)).json();
  if (path === "fresh") {
    expect(Object.keys(versions.execution.tasks)).toEqual(["main-research"]);
    const before = versions.execution.skills.filter((s: { sequence: number }) => s.sequence < versions.execution.first_web_sequence);
    expect([...new Set(before.map((s: { key: string }) => s.key))]).toEqual(skills);
    expect(versions.execution.physical_fetches).toBeGreaterThan(0);
    expect(versions.execution.physical_fetches).toBeLessThanOrEqual(12);
  }
  if (path === "derived") {
    expect(versions.parent.parent_run_id).toBe(process.env.E2E_COMPETITOR_PARENT);
    const original = process.env.E2E_COMPETITOR_ORIGINAL_PARENT || versions.parent.parent_run_id;
    const parent = await (await api.get(`/api/research/runs/${original}`)).json();
    expect(parent.output.completion_status).toBe("partial");
  }
  const pub = await api.post(`/api/research/runs/${runId}/publications`, { data: { format: "markdown" } });
  expect(pub.ok(), await pub.text()).toBe(true);
  const job = await pub.json();
  await expect.poll(async () => (await (await api.get(`/api/research/runs/${runId}/publications/${job.publication_id}`)).json()).status,
    { timeout: 120000 }).toBe("completed");
  const downloaded = await api.get(`/api/research/runs/${runId}/publications/${job.publication_id}/download`);
  expect(downloaded.ok()).toBe(true);
  expect(await downloaded.body()).toEqual(Buffer.from(final.markdown));
  const evidence = info.outputPath("verified-delivery.json");
  await writeFile(evidence, JSON.stringify({ runId, path, sha256: sha(final.markdown), bytes: (await downloaded.body()).length,
    review: final.report_review?.decision, completion: final.completion_status }, null, 2));
  await info.attach("verified-delivery", { path: evidence, contentType: "application/json" });
});
