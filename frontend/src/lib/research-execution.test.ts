import { describe, expect, it } from "vitest";
import { researchExecutionConfig, researchExecutionMode } from "./research-execution";

describe("research execution selection", () => {
  it("uses the adaptive default while retaining an explicit team preference", () => {
    expect(researchExecutionMode({ enable_async_research: true })).toBe("adaptive");
    expect(researchExecutionMode({ enable_async_research: true, async_research_mode: "teams" })).toBe("teams");
  });
  it("serializes each selection consistently and clears teams when choosing adaptive", () => {
    for (const mode of ["adaptive", "sync", "collaborator", "teams"] as const) {
      expect(researchExecutionMode(researchExecutionConfig(mode))).toBe(mode);
    }
    expect(researchExecutionConfig("adaptive").enable_async_research).toBe(false);
  });
});
