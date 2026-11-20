import { describe, expect, it } from "vitest";
import { formatBusinessTime } from "./business-time";

describe("business time", () => {
  it("shows the same Shanghai wall time for old and current responses", () => {
    const instant = "2026-11-18T16:00:01.987Z";
    const expected = "2026-11-19 00:00:01";
    expect(formatBusinessTime(instant)).toBe(expected);
    expect(formatBusinessTime(Date.parse(instant))).toBe(expected);
    expect(formatBusinessTime(expected)).toBe(expected);
  });

  it("keeps an absent or invalid time out of the UI", () => {
    expect(formatBusinessTime(null)).toBe("—");
    expect(formatBusinessTime("invalid")).toBe("—");
  });
});
