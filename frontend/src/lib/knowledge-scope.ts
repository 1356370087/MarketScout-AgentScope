import type { KnowledgeSearchRequest } from "./contracts/knowledge";
import type { MaterialRetrievalOptions } from "./contracts/documents";

export type MaterialScope = MaterialRetrievalOptions & { kb_ids: string[]; collection_ids: string[]; document_ids: string[] };

export function materialScopeFromParams(params: Pick<URLSearchParams, "getAll" | "get">): MaterialScope {
  return {
    kb_ids: [...new Set([...params.getAll("kb"), ...params.getAll("kb_id")])],
    collection_ids: params.getAll("collection"), document_ids: params.getAll("document"),
    version_mode: params.get("as_of") ? "as_of" : "current",
    as_of_published: params.get("as_of"), as_of_valid: params.get("valid_on"),
    profile_version: params.get("profile"), filters: {
      doc_types: params.getAll("doc_type"), languages: params.getAll("language"), entity_ids: params.getAll("entity"),
      ...(params.get("published_from") ? { publish_date_start: params.get("published_from")! } : {}),
      ...(params.get("published_to") ? { publish_date_end: params.get("published_to")! } : {}),
    },
  };
}

export function researchFromKnowledgeHref(request: KnowledgeSearchRequest): string {
  const params = new URLSearchParams({ query: request.query });
  for (const [key, values] of [["kb", request.kb_ids], ["collection", request.collection_ids], ["document", request.document_ids],
    ["doc_type", request.filters?.doc_types], ["language", request.filters?.languages], ["entity", request.filters?.entity_ids]] as const) {
    values?.forEach((value) => params.append(key, value));
  }
  if (request.version_mode === "as_of" && request.as_of_published) params.set("as_of", request.as_of_published);
  if (request.as_of_valid) params.set("valid_on", request.as_of_valid);
  if (request.profile_version) params.set("profile", request.profile_version);
  if (request.filters?.publish_date_start) params.set("published_from", request.filters.publish_date_start);
  if (request.filters?.publish_date_end) params.set("published_to", request.filters.publish_date_end);
  return `/research/new?${params}`;
}
