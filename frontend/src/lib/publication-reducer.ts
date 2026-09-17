import type { PublicationEvent, PublicationJob, PublicationStatus } from "./contracts/publications";

export interface PublicationState {
  jobsById: Record<string, PublicationJob>;
  lastEventId: number;
}

export function emptyPublicationState(items: PublicationJob[] = []): PublicationState {
  return {
    jobsById: Object.fromEntries(items.map((item) => [item.publication_id, item])),
    lastEventId: 0,
  };
}

export function publicationEventNeedsRefetch(
  state: PublicationState,
  event: PublicationEvent,
): boolean {
  return !state.jobsById[event.publication_id]
    || event.type === "publication.completed"
    || event.type === "publication.failed";
}

export function reducePublicationEvent(state: PublicationState, event: PublicationEvent): PublicationState {
  if (event.sequence <= state.lastEventId) return state;
  const existing = state.jobsById[event.publication_id];
  if (!existing) return { ...state, lastEventId: event.sequence };
  const payload = event.payload;
  const status = payload.status;
  const next: PublicationJob = {
    ...existing,
    status: (status === "queued" || status === "running" || status === "completed" || status === "failed" ? status : existing.status) as PublicationStatus,
    attempt: typeof payload.attempt === "number" ? payload.attempt : existing.attempt,
    error_code: payload.error_code === null
      ? null
      : typeof payload.error_code === "string"
        ? payload.error_code
        : existing.error_code,
    retryable: typeof payload.retryable === "boolean" ? payload.retryable : existing.retryable,
  };
  if (event.type === "publication.completed") {
    next.retryable = false;
    next.error_code = null;
    const filename = typeof payload.filename === "string" ? payload.filename : existing.artifact?.filename;
    const mediaType = typeof payload.media_type === "string" ? payload.media_type : existing.artifact?.media_type;
    const sizeBytes = typeof payload.size_bytes === "number" ? payload.size_bytes : existing.artifact?.size_bytes;
    const sha256 = typeof payload.sha256 === "string" ? payload.sha256 : existing.artifact?.sha256;
    if (filename && mediaType && typeof sizeBytes === "number" && sha256) {
      next.artifact = {
        ...existing.artifact,
        filename,
        media_type: mediaType,
        size_bytes: sizeBytes,
        sha256,
        ...(typeof payload.page_count === "number" ? { page_count: payload.page_count } : {}),
        ...(typeof payload.slide_count === "number" ? { slide_count: payload.slide_count } : {}),
        ...(typeof payload.download_url === "string" ? { download_url: payload.download_url } : {}),
      };
      next.download_url = typeof payload.download_url === "string" ? payload.download_url : next.download_url;
    }
  }
  return { jobsById: { ...state.jobsById, [event.publication_id]: next }, lastEventId: event.sequence };
}
