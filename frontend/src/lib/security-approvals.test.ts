import { describe, expect, it } from "vitest";
import { shouldRequestSecurityApprovals } from "./security-approvals";

describe("shouldRequestSecurityApprovals", () => {
  it("waits for the requested run snapshot before loading approvals", () => {
    expect(shouldRequestSecurityApprovals("run-2", "run-1", true, false)).toBe(false);
    expect(shouldRequestSecurityApprovals("run-2", "run-2", false, false)).toBe(false);
  });

  it("does not load approvals for terminal runs", () => {
    expect(shouldRequestSecurityApprovals("run-1", "run-1", true, true)).toBe(false);
  });

  it("loads approvals only for the hydrated active run", () => {
    expect(shouldRequestSecurityApprovals("run-1", "run-1", true, false)).toBe(true);
  });
});
