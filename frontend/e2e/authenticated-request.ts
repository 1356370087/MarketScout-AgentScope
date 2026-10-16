import type { APIRequestContext, Page } from "@playwright/test";

// Chromium accepts Secure cookies on localhost HTTP. APIRequestContext does
// not, so carry the browser session explicitly when testing a local production build.
export function authenticatedRequest(page: Page) {
  async function headers() {
    const cookies = await page.context().cookies();
    return {
      Cookie: cookies.map(({ name, value }) => `${name}=${value}`).join("; "),
      "X-CSRF-Token": cookies.find(({ name }) => name === "odr.csrf")?.value ?? "",
    };
  }
  return {
    async get(url: string) {
      return page.request.get(url, { headers: await headers() });
    },
    async post(url: string, options: Parameters<APIRequestContext["post"]>[1] = {}) {
      return page.request.post(url, { ...options, headers: { ...await headers(), ...options.headers } });
    },
  };
}
