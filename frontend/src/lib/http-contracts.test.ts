import { describe, expect, it } from "vitest";
import { httpContractSamples } from "../test/http-contract-samples";
import { emptyRunState, hydrateSnapshot } from "./run-reducer";
import { useResearchRunStore } from "../stores/research-run-store";

describe("native HTTP snapshots", () => {
  it("normalizes nullable HTTP fields at the state boundary", () => {
    const state = hydrateSnapshot(emptyRunState(), httpContractSamples.snapshots[0]);
    expect(state.status).toBe("pending");
    expect(state.currentStage).toBeUndefined();
    expect(state.pendingHumanAction).toBeUndefined();
    expect(state.publicationTheme).toBeUndefined();
    expect(state.qualityGate).toBeUndefined();
    expect(state.resultStatus).toBeUndefined();
    expect(state.terminationReason).toBeUndefined();
  });

  it("hydrates the shared store through the same reducer and resets old run data", () => {
    const snapshot = httpContractSamples.snapshots[1];
    useResearchRunStore.getState().reset(snapshot.run_id);
    useResearchRunStore.getState().hydrate(snapshot);
    const state = useResearchRunStore.getState();
    expect(state.report).toBe("# Sample report");
    expect(state.terminal).toBe(true);
    expect(state.connectionState).toBe("closed");
    expect(Object.values(state.stageProgress)).toEqual(Array(6).fill("completed"));
    useResearchRunStore.getState().reset("next-run");
    expect(useResearchRunStore.getState().report).toBe("");
    expect(useResearchRunStore.getState().terminal).toBe(false);
    expect(useResearchRunStore.getState().runId).toBe("next-run");
  });
});
