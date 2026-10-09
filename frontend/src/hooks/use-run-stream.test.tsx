import { createElement, type PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { researchApi, subscribeToRun } from "@/lib/api";
import type { PublicEvent, RunSnapshot } from "@/lib/types";
import { httpContractSamples } from "@/test/http-contract-samples";
import { useResearchRunStore } from "@/stores/research-run-store";
import { useRunStream } from "./use-run-stream";

vi.mock("@/lib/api", () => ({ researchApi: { getRun: vi.fn() }, subscribeToRun: vi.fn() }));

const snapshot = (status: RunSnapshot["status"], runId = "run-1") => ({
  ...httpContractSamples.snapshots[0], run_id: runId, status, last_event_id: 7,
}) as RunSnapshot;
const completed = { type: "run.completed", run_id: "run-1", sequence: 8,
  event_id: "event-8", timestamp: "2026-10-08T04:19:00Z", payload: {} } as PublicEvent;

function renderStream() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderHook(({ runId }) => useRunStream(runId), {
    initialProps: { runId: "run-1" },
    wrapper: ({ children }: PropsWithChildren) => createElement(QueryClientProvider, { client }, children),
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(researchApi.getRun).mockResolvedValue(snapshot("running"));
  vi.mocked(subscribeToRun).mockImplementation(async (options) => { options.onOpen(); });
});
afterEach(cleanup);

it("closes before the final snapshot and ignores the following EOF reconnect", async () => {
  let finish!: (value: RunSnapshot) => void;
  vi.mocked(researchApi.getRun).mockResolvedValueOnce(snapshot("running"))
    .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  renderStream();
  await waitFor(() => expect(subscribeToRun).toHaveBeenCalledTimes(1));
  const options = vi.mocked(subscribeToRun).mock.calls[0][0];
  await act(async () => { options.onEvent(completed); options.onReconnect(); });
  expect(options.signal.aborted).toBe(true);
  expect(useResearchRunStore.getState().connectionState).toBe("closed");
  expect(useResearchRunStore.getState().isReconnecting).toBe(false);
  await act(async () => { finish(snapshot("completed")); });
  expect(subscribeToRun).toHaveBeenCalledTimes(1);
});

it("keeps the terminal event when the final snapshot GET fails", async () => {
  vi.mocked(researchApi.getRun).mockResolvedValueOnce(snapshot("running")).mockRejectedValueOnce(new Error("502"));
  renderStream();
  await waitFor(() => expect(subscribeToRun).toHaveBeenCalledTimes(1));
  await act(async () => { await vi.mocked(subscribeToRun).mock.calls[0][0].onEvent(completed); });
  expect(useResearchRunStore.getState().terminal).toBe(true);
  expect(useResearchRunStore.getState().connectionState).toBe("closed");
});

it("does not hydrate an old run after the user changes runs", async () => {
  let finish!: (value: RunSnapshot) => void;
  vi.mocked(researchApi.getRun).mockResolvedValueOnce(snapshot("running"))
    .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
    .mockResolvedValueOnce(snapshot("running", "run-2"));
  const { rerender } = renderStream();
  await waitFor(() => expect(subscribeToRun).toHaveBeenCalledTimes(1));
  await act(async () => { vi.mocked(subscribeToRun).mock.calls[0][0].onEvent(completed); });
  rerender({ runId: "run-2" });
  await waitFor(() => expect(useResearchRunStore.getState().runId).toBe("run-2"));
  await act(async () => { finish(snapshot("completed")); });
  expect(useResearchRunStore.getState().runId).toBe("run-2");
});
