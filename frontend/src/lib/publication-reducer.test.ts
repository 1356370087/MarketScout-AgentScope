import { describe, expect, it } from "vitest";
import {
  emptyPublicationState,
  publicationEventNeedsRefetch,
  reducePublicationEvent,
} from "./publication-reducer";
import type { PublicationJob } from "./types";

const job: PublicationJob = {
  publication_id: "pub-1", run_id: "run-1", requested_format: "pdf", format: "pdf", status: "queued",
  report_sha256: "a".repeat(64), theme: { preset: "default", primary_color: "#0F766E", accent_color: "#F6BD60", font_family: "cjk_sans", locale: "zh-CN", footer_text: "", pdf_page_size: "a4", pptx_aspect_ratio: "16:9" },
  theme_sha256: "b".repeat(64), attempt: 0, max_attempts: 3, retryable: true, status_url: "/status", events_url: "/events", created_at: 1, updated_at: 1,
};

describe("publication reducer", () => {
  it("applies monotonic lifecycle events and ignores replays", () => {
    let state = emptyPublicationState([job]);
    state = reducePublicationEvent(state, { schema_version: 1, event_id: "e1", sequence: 1, run_id: "run-1", publication_id: "pub-1", type: "publication.started", timestamp: 1, payload: { status: "running", attempt: 1 } });
    expect(state.jobsById["pub-1"].status).toBe("running");
    expect(reducePublicationEvent(state, { schema_version: 1, event_id: "e1", sequence: 1, run_id: "run-1", publication_id: "pub-1", type: "publication.queued", timestamp: 1, payload: { status: "queued" } })).toBe(state);
  });

  it("advances the cursor for unknown jobs so the hook can refetch once", () => {
    const event = {
      schema_version: 1, event_id: "e2", sequence: 2, run_id: "run-1", publication_id: "pub-2",
      type: "publication.queued", timestamp: 2, payload: { status: "queued", attempt: 0 },
    } as const;
    const initial = emptyPublicationState([job]);
    const state = reducePublicationEvent(initial, event);
    expect(publicationEventNeedsRefetch(initial, event)).toBe(true);
    expect(state.lastEventId).toBe(2);
    expect(state.jobsById["pub-2"]).toBeUndefined();
  });

  it("refetches terminal events but not known intermediate transitions", () => {
    const state = emptyPublicationState([job]);
    const started = { schema_version: 1, event_id: "e3", sequence: 3, run_id: "run-1", publication_id: "pub-1", type: "publication.started", timestamp: 3, payload: { status: "running" } } as const;
    const completed = { ...started, event_id: "e4", sequence: 4, type: "publication.completed" } as const;
    expect(publicationEventNeedsRefetch(state, started)).toBe(false);
    expect(publicationEventNeedsRefetch(state, completed)).toBe(true);
  });
});
