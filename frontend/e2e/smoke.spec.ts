import { expect, test } from "@playwright/test";

test.skip(
  process.env.NEXT_PUBLIC_LOCAL_DEV_AUTH_BYPASS !== "true",
  "This scenario only applies when the explicit local authentication bypass is enabled.",
);

// Keep intake checks independent of backend authentication and model services.
test.beforeEach(async ({ page }) => {
  await page.route("**/api/research/runs**", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/research/capabilities", (route) => route.fulfill({ json: { defaults: {}, editable_config_keys: [], features: {} } }));
});

test("local bypass opens the research intake at all breakpoints", async ({ page }) => {
  await page.goto("/research/new");
  await expect(page.getByRole("heading", { name: /今天，想深入了解什么/ })).toBeVisible();
  await expect(page.getByLabel("研究问题")).toBeVisible();
});

test("interface theme defaults to light and persists an explicit dark choice", async ({ page }) => {
  await page.goto("/research/new");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.getByRole("button", { name: "切换到深色模式" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});
