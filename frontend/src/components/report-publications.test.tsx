import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePublications } from "@/hooks/use-publications";
import { researchApi } from "@/lib/api";
import { defaultPublicationTheme } from "@/lib/settings";
import { ReportPublications } from "./report-publications";

vi.mock("@/hooks/use-publications", () => ({ usePublications: vi.fn() }));

const mockedUsePublications = vi.mocked(usePublications);

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><ReportPublications runId="run-1" preferredFormat="pdf" /></QueryClientProvider>);
}

describe("ReportPublications", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    mockedUsePublications.mockReturnValue({
      data: { run_id: "run-1", events_url: "/events", worker: "ready", items: [] },
    } as unknown as ReturnType<typeof usePublications>);
  });

  afterEach(() => cleanup());

  it("creates an actual publication with the bounded theme", async () => {
    const create = vi.spyOn(researchApi, "createPublication").mockResolvedValue({
      publication_id: "pub-1", run_id: "run-1", requested_format: "pdf", format: "pdf", status: "queued",
      report_sha256: "a".repeat(64), theme: defaultPublicationTheme, theme_sha256: "b".repeat(64), attempt: 0,
      max_attempts: 3, retryable: true, status_url: "/status", events_url: "/events", created_at: 1, updated_at: 1,
    });
    renderPanel();
    await act(async () => { fireEvent.click(screen.getByTitle("生成 PDF")); });
    expect(create).toHaveBeenCalledWith("run-1", "pdf", undefined);
  });

  it("renders a download link only for completed output", () => {
    mockedUsePublications.mockReturnValue({
      data: { run_id: "run-1", events_url: "/events", worker: "ready", items: [{
        publication_id: "pub-1", run_id: "run-1", requested_format: "pdf", format: "pdf", status: "completed",
        report_sha256: "a".repeat(64), theme: defaultPublicationTheme, theme_sha256: "b".repeat(64), attempt: 1,
        max_attempts: 3, retryable: false, status_url: "/status", events_url: "/events", download_url: "/download",
        artifact: { filename: "report.pdf", media_type: "application/pdf", size_bytes: 1024, sha256: "c".repeat(64) },
        created_at: 1, updated_at: 2,
      }] },
    } as unknown as ReturnType<typeof usePublications>);
    vi.spyOn(researchApi, "publicationDownloadUrl").mockReturnValue("/api/research/download");
    renderPanel();
    expect(screen.getByTitle("下载 PDF")).toHaveAttribute("href", "/api/research/download");
  });

  it("keeps the previous successful file visible when the latest attempt fails", () => {
    mockedUsePublications.mockReturnValue({
      data: {
        run_id: "run-1", events_url: "/events", worker: "ready", items: [
          {
            publication_id: "pub-failed", run_id: "run-1", requested_format: "pdf", format: "pdf", status: "failed",
            report_sha256: "a".repeat(64), theme: defaultPublicationTheme, theme_sha256: "b".repeat(64), attempt: 2,
            max_attempts: 3, retryable: true, error_code: "publisher_render_failed", status_url: "/status", events_url: "/events",
            created_at: 3, updated_at: 3,
          },
          {
            publication_id: "pub-success", run_id: "run-1", requested_format: "pdf", format: "pdf", status: "completed",
            report_sha256: "c".repeat(64), theme: defaultPublicationTheme, theme_sha256: "d".repeat(64), attempt: 1,
            max_attempts: 3, retryable: false, status_url: "/status", events_url: "/events", download_url: "/runs/run-1/publications/pub-success/download",
            artifact: { filename: "report.pdf", media_type: "application/pdf", size_bytes: 1024, sha256: "e".repeat(64) },
            created_at: 2, updated_at: 2,
          },
        ],
      },
    } as unknown as ReturnType<typeof usePublications>);
    renderPanel();
    expect(screen.getByTitle("重试生成")).toBeInTheDocument();
    expect(screen.getByTitle("下载上一版 PDF")).toHaveAttribute(
      "href",
      "/api/research/runs/run-1/publications/pub-success/download",
    );
  });

  it("isolates the theme draft when switching Runs", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const view = render(
      <QueryClientProvider client={client}>
        <ReportPublications runId="run-1" defaultTheme={defaultPublicationTheme} />
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByDisplayValue("Default"), { target: { value: "academic" } });
    expect(screen.getByDisplayValue("Academic")).toBeInTheDocument();

    view.rerender(
      <QueryClientProvider client={client}>
        <ReportPublications runId="run-2" defaultTheme={defaultPublicationTheme} />
      </QueryClientProvider>,
    );
    expect(screen.getByDisplayValue("Default")).toBeInTheDocument();
  });
});
