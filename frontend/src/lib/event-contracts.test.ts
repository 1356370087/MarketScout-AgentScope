import { describe, expect, it } from "vitest";
import { eventContractSamples } from "../test/event-contract-samples";
import { emptyRunState, reducePublicEvent } from "./run-reducer";
import { emptyTaskActivityState, reduceTaskActivity } from "./task-activity-reducer";
import { emptyPublicationState, publicationEventNeedsRefetch, reducePublicationEvent } from "./publication-reducer";

describe("server-generated SSE contracts", () => {
  it("maps nine internal steps to six public progress stages", () => {
    const stages = ["preparing", "preparing", "planning", "planning", "planning", "researching", "synthesizing", "writing", "finalizing"];
    let state = emptyRunState("run-1");
    eventContractSamples.stageEvents.forEach((event, index) => {
      expect(event.stage).toBe(stages[index]);
      expect(event.payload.stage_id).toBe(stages[index]);
      expect(event.payload.stage_count).toBe(6);
      state = reducePublicEvent(state, event);
      expect(state.currentStage).toBe(stages[index]);
      expect(state.stageProgress[event.stage]).toBe("running");
    });
    expect(Object.keys(state.stageProgress)).toEqual([...new Set(stages)]);
  });

  it("feeds the public run reducer without framework event conversion", () => {
    const events = eventContractSamples.runEvents;
    const state = events.reduce(reducePublicEvent, emptyRunState("run-1"));
    expect(state.status).toBe("completed");
    expect(state.lastEventId).toBe(2);
    expect(reducePublicEvent(state, events[0])).toBe(state);
    expect(events[0].stage).toBeNull();
  });

  it("retains absent task metrics and rejects duplicate or stale events", () => {
    const events = eventContractSamples.taskEvents;
    const state = reduceTaskActivity(emptyTaskActivityState(), [...events].reverse());
    expect(state.events.map((event) => event.sequence)).toEqual([1, 2]);
    const replayed = reduceTaskActivity(state, events);
    expect(replayed.events).toEqual(state.events);
    expect(replayed.cursor).toBe(2);
    expect(replayed.events[0].duration_ms).toBeNull();
    expect(replayed.events[0].iteration).toBeNull();
  });

  it("advances the independent publication cursor and requests unknown job snapshots", () => {
    const events = eventContractSamples.publicationEvents;
    const initial = emptyPublicationState();
    expect(publicationEventNeedsRefetch(initial, events[0])).toBe(true);
    const state = events.reduce(reducePublicationEvent, initial);
    expect(state.lastEventId).toBe(2);
    expect(reducePublicationEvent(state, events[0])).toBe(state);
    expect(events[0].timestamp).toBeTypeOf("number");
  });
});
