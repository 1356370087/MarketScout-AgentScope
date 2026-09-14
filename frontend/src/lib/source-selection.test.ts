import { describe, expect, it } from "vitest";
import { buildSourceRefs, sourceSelectionIsValid } from "@/lib/source-selection";

describe("research source selection", () => {
  it("keeps the backward-compatible web payload empty", () => {
    const refs = buildSourceRefs("web", [], "", "");
    expect(refs).toEqual([]);
    expect(sourceSelectionIsValid("web", refs)).toBe(true);
  });

  it.each(["documents", "hybrid"] as const)(
    "%s requires a selected document",
    (mode) => {
      expect(sourceSelectionIsValid(mode, buildSourceRefs(mode, [], "", ""))).toBe(false);
      const refs = buildSourceRefs(mode, ["doc-1"], "", "");
      expect(refs).toEqual([{ type: "document", id: "doc-1" }]);
      expect(sourceSelectionIsValid(mode, refs)).toBe(true);
    },
  );

  it("builds a bounded specific payload from files, URLs and domains", () => {
    const refs = buildSourceRefs(
      "specific",
      ["doc-1"],
      " https://example.com/report,\nhttps://example.com/data ",
      " example.com, docs.example.com ",
    );
    expect(refs).toEqual([
      { type: "document", id: "doc-1" },
      { type: "url", url: "https://example.com/report" },
      { type: "url", url: "https://example.com/data" },
      { type: "domain", domain: "example.com" },
      { type: "domain", domain: "docs.example.com" },
    ]);
    expect(sourceSelectionIsValid("specific", refs)).toBe(true);
  });

  it("rejects an empty specific selection", () => {
    expect(
      sourceSelectionIsValid("specific", buildSourceRefs("specific", [], "  ", "\n")),
    ).toBe(false);
  });
});
