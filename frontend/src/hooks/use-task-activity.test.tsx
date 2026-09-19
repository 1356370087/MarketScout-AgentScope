import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { researchApi, subscribeToTaskActivity } from "@/lib/api";
import { useTaskActivity } from "./use-task-activity";
import type { TaskActivityPage, TaskActivityEvent } from "@/lib/contracts/research";
import { eventContractSamples } from "@/test/event-contract-samples";

vi.mock("@/lib/api", () => ({ researchApi: { taskActivity: vi.fn() }, subscribeToTaskActivity: vi.fn() }));

const page = (cursor: number) => ({ items: [],
  last_event_id: cursor, oldest_sequence: 0, has_more: false, source: "native", detail_level: "summary", stream_url: "/runs/run-1/tasks/task-1/activity/stream",
}) satisfies TaskActivityPage;

function renderActivity() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderHook(() => useTaskActivity("run-1", "task-1"), {
    wrapper: ({ children }: PropsWithChildren) => createElement(QueryClientProvider, { client }, children),
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(researchApi.taskActivity).mockResolvedValue(page(7));
  vi.mocked(subscribeToTaskActivity).mockImplementation(async (options) => {
    options.onOpen();
    await new Promise<void>((resolve) => options.signal.addEventListener("abort", () => resolve(), { once: true }));
  });
});
afterEach(cleanup);

describe("task activity connection", () => {
  it("does not reconnect a history snapshot that already contains the terminal event", async () => {
    vi.mocked(researchApi.taskActivity).mockResolvedValue({ ...page(2), items: eventContractSamples.taskEvents });
    const { result } = renderActivity();
    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(result.current.connection).toBe("closed");
    expect(subscribeToTaskActivity).not.toHaveBeenCalled();
  });

  it("reconnects after auth refresh and limits repeated refresh restarts", async () => {
    vi.mocked(subscribeToTaskActivity).mockRejectedValue(new Error("task-sse-auth-refreshed"));
    const { result } = renderActivity();
    await waitFor(() => expect(result.current.connection).toBe("error"));
    expect(subscribeToTaskActivity).toHaveBeenCalledTimes(2);
  });

  it("restarts from the fetched cursor after a 409", async () => {
    vi.mocked(researchApi.taskActivity).mockResolvedValueOnce(page(7)).mockResolvedValue(page(3));
    vi.mocked(subscribeToTaskActivity).mockImplementationOnce(async (options) => {
      await options.onCursorAhead();
      throw new Error("task-cursor-ahead");
    });
    const { result } = renderActivity();
    await waitFor(() => expect(result.current.connection).toBe("connected"));
    expect(vi.mocked(subscribeToTaskActivity).mock.calls.map(([options]) => options.after)).toEqual([7, 3]);
  });

  it.each(["task.completed", "task.failed", "task.cancelled", "task.timed_out"])("closes transport at %s", async (type) => {
    vi.mocked(subscribeToTaskActivity).mockImplementationOnce(async (options) => {
      options.onEvent({ schema_version: 1, event_id: "event-8", sequence: 8, run_id: "run-1", task_id: "task-1",
        timestamp: "2026-09-19T00:00:00Z", type, kind: "lifecycle", phase: "terminal", status: "success",
        title: "Done", summary: "Done", payload: {},
      } satisfies TaskActivityEvent);
      expect(options.signal.aborted).toBe(true);
    });
    const { result } = renderActivity();
    await waitFor(() => expect(result.current.connection).toBe("closed"));
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    expect(subscribeToTaskActivity).toHaveBeenCalledTimes(1);
  });

  it("aborts the active stream on unmount", async () => {
    const { result, unmount } = renderActivity();
    await waitFor(() => expect(result.current.connection).toBe("connected"));
    const signal = vi.mocked(subscribeToTaskActivity).mock.calls[0][0].signal;
    unmount();
    expect(signal.aborted).toBe(true);
  });
});
