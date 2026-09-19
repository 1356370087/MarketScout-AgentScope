import type { DocumentChunk, ResearchDocument } from "../contracts/documents";
import { apiFetch } from "./http";

const DOCUMENT_PAGE_SIZE = 100;

type DocumentListParams = { q?: string; status?: string; limit?: number; offset?: number };

const listDocumentsPage = (params: DocumentListParams = {}) => {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); });
  return apiFetch<{ items: ResearchDocument[]; total: number }>(`/documents${query.size ? `?${query}` : ""}`);
};

export async function listAllDocuments(params: Omit<DocumentListParams, "limit" | "offset"> = {}) {
  // The API caps one page at 100; walk the bounded result set for selectors
  // that need every ready document.
  const items: ResearchDocument[] = [];
  let offset = 0;
  let total = 0;
  while (true) {
    const page = await listDocumentsPage({ ...params, limit: DOCUMENT_PAGE_SIZE, offset });
    total = page.total;
    items.push(...page.items);
    if (!page.items.length || items.length >= total || page.items.length < DOCUMENT_PAGE_SIZE) break;
    offset += page.items.length;
  }
  return { items, total };
}

export const documentsApi = {
  listDocuments: listDocumentsPage,
  listAllDocuments: (params: Omit<DocumentListParams, "limit" | "offset"> = {}) => listAllDocuments(params),
  getDocument: (id: string) => apiFetch<ResearchDocument>(`/documents/${encodeURIComponent(id)}`),
  documentChunks: (id: string) => apiFetch<{ items: DocumentChunk[] }>(`/documents/${encodeURIComponent(id)}/chunks`),
  uploadDocument: async (file: File) => {
    const form = new FormData(); form.set("file", file);
    return apiFetch<{ document: ResearchDocument; deduplicated: boolean }>("/documents", { method: "POST", body: form });
  },
  retryDocument: (id: string) => apiFetch<ResearchDocument>(`/documents/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  reindexDocument: (id: string) => apiFetch<ResearchDocument>(`/documents/${encodeURIComponent(id)}/reindex`, { method: "POST" }),
  deleteDocument: (id: string) => apiFetch<{ id: string; status: string }>(`/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
