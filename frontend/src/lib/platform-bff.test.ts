import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest, NextResponse } from "next/server";
import { GET as platformGet, POST as platformPost } from "@/app/api/platform/[...path]/route";
import { POST as setupPost } from "@/app/api/setup/[action]/route";
import { authenticatedProxy, sameOriginValid } from "./server-auth";

vi.mock("./server-auth", () => ({
  authenticatedProxy: vi.fn(async () => NextResponse.json({ ok: true })),
  sameOriginValid: vi.fn(() => true),
  backendOrigin: "http://127.0.0.1:2024",
}));

beforeEach(() => { vi.clearAllMocks(); vi.mocked(sameOriginValid).mockReturnValue(true); });

describe("platform BFF boundaries", () => {
  it("forwards configuration writes through the authenticated proxy", async () => {
    const request = new NextRequest("http://localhost/api/platform/configurations/revision/publish", { method: "POST" });
    await platformPost(request, { params: Promise.resolve({ path: ["configurations", "revision", "publish"] }) });
    expect(authenticatedProxy).toHaveBeenCalledWith(request, "/platform/configurations/revision/publish");
  });

  it("rejects unknown roots and path traversal before forwarding credentials", async () => {
    for (const path of [["auth", "me"], ["configurations", "..", "credentials"], ["models", "nested/path"], ["models", "nested\\path"]]) {
      const response = await platformGet(new NextRequest("http://localhost/api/platform/test"), { params: Promise.resolve({ path }) });
      expect(response.status).toBe(404);
    }
    expect(authenticatedProxy).not.toHaveBeenCalled();
  });

  it("rejects cross-origin initialization even before a user session exists", async () => {
    vi.mocked(sameOriginValid).mockReturnValue(false);
    const fetch = vi.spyOn(globalThis, "fetch");
    const response = await setupPost(new NextRequest("http://localhost/api/setup/bootstrap", { method: "POST" }), { params: Promise.resolve({ action: "bootstrap" }) });
    expect(response.status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
    fetch.mockRestore();
  });
});
