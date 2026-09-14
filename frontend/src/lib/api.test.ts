import { afterEach, describe, expect, it, vi } from "vitest";
import { listAllDocuments, researchApi } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("document pagination", () => {
  it("loads every page instead of silently stopping at 100 documents", async () => {
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      calls.push(url);
      const offset = Number(new URL(url, "http://localhost").searchParams.get("offset") ?? "0");
      const count = offset === 0 ? 100 : 2;
      const items = Array.from({ length: count }, (_, index) => ({
        id: `doc-${offset + index}`,
        filename: `doc-${offset + index}.txt`,
        media_type: "text/plain",
        size_bytes: 1,
        sha256: "a".repeat(64),
        status: "ready",
        chunk_count: 1,
        ocr_pages: 0,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      }));
      return new Response(JSON.stringify({ items, total: 102 }), { status: 200, headers: { "Content-Type": "application/json" } });
    });

    const result = await listAllDocuments();

    expect(result.items).toHaveLength(102);
    expect(calls.map((url) => new URL(url, "http://localhost").searchParams.get("offset"))).toEqual(["0", "100"]);
  });
});

describe("publication download URLs", () => {
  it("uses the server path while routing it through the BFF prefix", () => {
    const base = (process.env.NEXT_PUBLIC_RESEARCH_API_BASE ?? "/api/research").replace(/\/+$/, "");
    expect(researchApi.publicationDownloadUrl(
      "run-1",
      "pub-1",
      "/runs/run-1/publications/pub-1/download",
    )).toBe(`${base}/runs/run-1/publications/pub-1/download`);
  });

  it("rejects a cross-origin server URL and falls back to the canonical endpoint", () => {
    const base = (process.env.NEXT_PUBLIC_RESEARCH_API_BASE ?? "/api/research").replace(/\/+$/, "");
    expect(researchApi.publicationDownloadUrl(
      "run-1",
      "pub-1",
      "https://downloads.example.invalid/report.pdf",
    )).toBe(`${base}/runs/run-1/publications/pub-1/download`);
  });
});
