import { describe, expect, it } from "vitest";
import { materialScopeFromParams, researchFromKnowledgeHref } from "./knowledge-scope";
import { sourceSelectionIsValid } from "./source-selection";

describe("knowledge research handoff", () => {
  it("preserves the submitted scope, dates and filters without turning it into web research", () => {
    const request = { query: "A & B 定价", kb_ids: ["team"], collection_ids: ["pricing"], document_ids: ["doc"],
      version_mode: "as_of" as const, as_of_published: "2026-01-02", as_of_valid: "2025-12-31",
      profile_version: "compare-v1", filters: { doc_types: ["价格表"], languages: ["zh"], entity_ids: ["company"] } };
    const url = new URL(researchFromKnowledgeHref(request), "https://fixture.test");
    expect(url.searchParams.get("query")).toBe(request.query);
    const { query, ...scope } = request;
    expect(url.searchParams.get("query")).toBe(query);
    expect(materialScopeFromParams(url.searchParams)).toMatchObject(scope);
  });
  it("accepts a knowledge base or collection as document-mode material", () => {
    expect(sourceSelectionIsValid("documents", [{ type: "collection", id: "pricing" }])).toBe(true);
    expect(sourceSelectionIsValid("hybrid", [{ type: "knowledge_base", id: "team" }])).toBe(true);
    expect(sourceSelectionIsValid("web", [{ type: "knowledge_base", id: "team" }])).toBe(false);
  });
});
