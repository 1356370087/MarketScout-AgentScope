import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

vi.mock("server-only", () => ({}));

describe("sameOriginValid", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
  });

  it("accepts requests without an Origin header (non-browser clients)", async () => {
    const { sameOriginValid } = await import("./server-auth");
    const request = new NextRequest("http://localhost:3000/api/auth/login", {
      method: "POST",
    });
    expect(sameOriginValid(request)).toBe(true);
  });

  it("accepts a matching same-origin request", async () => {
    const { sameOriginValid } = await import("./server-auth");
    const request = new NextRequest("http://localhost:3000/api/auth/login", {
      method: "POST",
      headers: { Origin: "http://localhost:3000" },
    });
    expect(sameOriginValid(request)).toBe(true);
  });

  it("rejects a foreign origin (login CSRF)", async () => {
    const { sameOriginValid } = await import("./server-auth");
    const request = new NextRequest("http://localhost:3000/api/auth/login", {
      method: "POST",
      headers: { Origin: "https://evil.example" },
    });
    expect(sameOriginValid(request)).toBe(false);
  });

  it("uses the browser-facing Host behind a standalone container", async () => {
    const { sameOriginValid } = await import("./server-auth");
    const request = new NextRequest("http://0.0.0.0:3000/api/auth/login", {
      method: "POST",
      headers: { Host: "localhost:3180", Origin: "http://localhost:3180" },
    });
    expect(sameOriginValid(request)).toBe(true);
  });

  it("does not trust a forged forwarded host to accept a foreign origin", async () => {
    const { sameOriginValid } = await import("./server-auth");
    const request = new NextRequest("http://0.0.0.0:3000/api/auth/login", {
      method: "POST",
      headers: {
        Host: "localhost:3180", Origin: "https://evil.example",
        "X-Forwarded-Host": "evil.example",
      },
    });
    expect(sameOriginValid(request)).toBe(false);
  });

  it("rejects a different port on the same host", async () => {
    const { sameOriginValid } = await import("./server-auth");
    const request = new NextRequest("http://localhost:3000/api/auth/login", {
      method: "POST",
      headers: { Origin: "http://localhost:3001" },
    });
    expect(sameOriginValid(request)).toBe(false);
  });

  it("rejects an unparseable Origin value", async () => {
    const { sameOriginValid } = await import("./server-auth");
    const request = new NextRequest("http://localhost:3000/api/auth/login", {
      method: "POST",
      headers: { Origin: "null" },
    });
    // "null" parses as a relative URL whose host is empty → mismatch.
    expect(sameOriginValid(request)).toBe(false);
  });
});

describe("authenticatedProxy local development bypass", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("NEXT_PUBLIC_LOCAL_DEV_AUTH_BYPASS", "true");
  });

  it("forwards mutation requests without auth cookies or CSRF", async () => {
    const upstream = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ run_id: "run-local" }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { authenticatedProxy } = await import("./server-auth");
    const request = new NextRequest("http://localhost/api/research/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: [] }),
    });

    const response = await authenticatedProxy(request, "/runs");

    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({ run_id: "run-local" });
    const [, init] = upstream.mock.calls[0];
    expect(new Headers(init?.headers).get("Authorization")).toBe(
      "Bearer local-dev-bypass",
    );
  });

  it("streams multipart uploads without buffering the body", async () => {
    const upstream = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ document: { id: "doc-local" } }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const form = new FormData();
    form.set("file", new File(["internal evidence"], "evidence.txt", { type: "text/plain" }));
    const { authenticatedProxy } = await import("./server-auth");
    const request = new NextRequest("http://localhost/api/research/documents", {
      method: "POST",
      body: form,
    });

    const response = await authenticatedProxy(request, "/documents");

    expect(response.status).toBe(202);
    const [, init] = upstream.mock.calls[0];
    expect(init?.body).toBeInstanceOf(ReadableStream);
    expect((init as RequestInit & { duplex?: string }).duplex).toBe("half");
    expect(new Headers(init?.headers).has("content-length")).toBe(false);
  });

  it("preserves a declared multipart length for the upstream limit gate", async () => {
    const upstream = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ document: { id: "doc-length" } }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const form = new FormData();
    form.set("file", new File(["internal evidence"], "evidence.txt", { type: "text/plain" }));
    const { authenticatedProxy } = await import("./server-auth");
    const request = new NextRequest("http://localhost/api/research/documents", {
      method: "POST",
      headers: { "Content-Length": "4096" },
      body: form,
    });

    await authenticatedProxy(request, "/documents");

    const [, init] = upstream.mock.calls[0];
    expect(new Headers(init?.headers).get("content-length")).toBe("4096");
  });

  it("streams publication downloads with their binary metadata intact", async () => {
    const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46]);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(bytes, {
        status: 200,
        headers: {
          "Content-Type": "application/pdf",
          "Content-Disposition": 'attachment; filename="report.pdf"',
          ETag: '"abc123"',
          "X-Content-Type-Options": "nosniff",
        },
      }),
    );
    const { authenticatedProxy } = await import("./server-auth");
    const request = new NextRequest(
      "http://localhost/api/research/runs/run-1/publications/pub-1/download",
    );

    const response = await authenticatedProxy(
      request,
      "/runs/run-1/publications/pub-1/download",
    );

    expect(response.status).toBe(200);
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(bytes);
    expect(response.headers.get("content-type")).toBe("application/pdf");
    expect(response.headers.get("content-disposition")).toBe(
      'attachment; filename="report.pdf"',
    );
    expect(response.headers.get("etag")).toBe('"abc123"');
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
  });
});
