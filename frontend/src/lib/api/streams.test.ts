import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchEventSource, type FetchEventSourceInit } from "@microsoft/fetch-event-source";
import { refreshBrowserSession } from "../auth";
import { subscribeToRun } from "./research";
import { subscribeToTaskActivity } from "./activity";
import { subscribeToPublications } from "./publications";

vi.mock("@microsoft/fetch-event-source", () => ({ fetchEventSource: vi.fn() }));
vi.mock("../auth", () => ({
  csrfHeaders: (extra?: HeadersInit) => new Headers(extra),
  refreshBrowserSession: vi.fn(),
}));

beforeEach(() => vi.resetAllMocks());

describe.each([
  ["run", subscribeToRun, "/runs/run-1/events?after=7"],
  ["task", subscribeToTaskActivity, "/runs/run-1/tasks/task-1/activity/stream?after=7"],
  ["publication", subscribeToPublications, "/runs/run-1/publications/events?after=7"],
] as const)("%s stream recovery", (_name, subscribe, path) => {
  async function setup() {
    const onEvent = vi.fn();
    const onReconnect = vi.fn();
    const onCursorAhead = vi.fn(async () => undefined);
    await subscribe({ runId: "run-1", taskId: "task-1", after: 7,
      signal: new AbortController().signal, onOpen: vi.fn(), onEvent, onReconnect, onCursorAhead });
    const [url, init] = vi.mocked(fetchEventSource).mock.calls[0];
    expect(url).toBe(`/api/research${path}`);
    expect(new Headers(init?.headers).get("Last-Event-ID")).toBe("7");
    return { init: init as FetchEventSourceInit, onEvent, onReconnect, onCursorAhead };
  }

  it("retries unexpected EOF with bounded backoff, ignores heartbeats", async () => {
    const { init, onEvent, onReconnect } = await setup();
    expect(() => init.onclose?.()).toThrow("sse-disconnected");
    expect(init.onerror?.(new Error("sse-disconnected"))).toBe(1000);
    expect(init.onerror?.(new Error("network"))).toBe(2000);
    for (let i = 0; i < 8; i++) init.onerror?.(new Error("network"));
    expect(init.onerror?.(new Error("network"))).toBe(30000);
    await init.onopen?.(new Response("", { status: 200 }));
    expect(init.onerror?.(new Error("network"))).toBe(1000);
    init.onmessage?.({ id: "", event: "", data: "" });
    expect(onEvent).not.toHaveBeenCalled();
    expect(onReconnect).toHaveBeenCalled();
  });

  it.each([401, 403, 404])("stops retrying rejected access (%s)", async (status) => {
    const { init, onReconnect } = await setup();
    vi.mocked(refreshBrowserSession).mockResolvedValue(false);
    const error = await init.onopen?.(new Response("", { status })).catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(Error);
    expect(() => init.onerror?.(error)).toThrow();
    expect(onReconnect).not.toHaveBeenCalled();
    expect(refreshBrowserSession).toHaveBeenCalledTimes(status === 401 ? 1 : 0);
  });

  it("hands a successful refresh back to the hook instead of using stale headers", async () => {
    const { init } = await setup();
    vi.mocked(refreshBrowserSession).mockResolvedValue(true);
    await expect(init.onopen?.(new Response("", { status: 401 }))).rejects.toThrow("sse-auth-refreshed");
    await expect(init.onopen?.(new Response("", { status: 401 }))).rejects.toThrow("sse-401");
    expect(refreshBrowserSession).toHaveBeenCalledTimes(1);
  });

  it("resynchronizes an ahead cursor before asking the hook to reconnect", async () => {
    const { init, onCursorAhead, onReconnect } = await setup();
    await expect(init.onopen?.(new Response("", { status: 409 }))).rejects.toThrow("cursor-ahead");
    expect(onCursorAhead).toHaveBeenCalledTimes(1);
    expect(onReconnect).not.toHaveBeenCalled();
  });
});
