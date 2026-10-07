import type { ResearchDocument, SourceMode, SourceRef } from "@/lib/types";

export function buildSourceRefs(
  mode: SourceMode,
  selectedDocuments: string[],
  urls: string,
  domains: string,
): SourceRef[] {
  const documents = selectedDocuments.map((id) => ({ type: "document" as const, id }));
  if (mode !== "specific") return documents;
  const urlRefs = urls
    .split(/[\n,]/)
    .map((value) => value.trim())
    .filter(Boolean)
    .map((url) => ({ type: "url" as const, url }));
  const domainRefs = domains
    .split(/[\n,]/)
    .map((value) => value.trim())
    .filter(Boolean)
    .map((domain) => ({ type: "domain" as const, domain }));
  return [...documents, ...urlRefs, ...domainRefs];
}

export function sourceSelectionIsValid(mode: SourceMode, refs: SourceRef[]): boolean {
  if (mode === "web") return refs.length === 0;
  if (mode === "specific") return refs.length > 0;
  return refs.some((source) => ["document", "knowledge_base", "collection"].includes(source.type));
}

export function isPublishedDocument(document: ResearchDocument): boolean {
  return !!document.current_generation_id && document.status !== "deleting";
}
