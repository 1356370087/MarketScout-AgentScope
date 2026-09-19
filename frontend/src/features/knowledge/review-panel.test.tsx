import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReviewPanel } from "./review-panel";
import { fetchGenerationReview, publishGeneration } from "@/lib/knowledge-api";

vi.mock("@/lib/knowledge-api", () => ({
  fetchGenerationReview: vi.fn(), publishGeneration: vi.fn(),
  confirmMetadata: vi.fn(), reviseUnit: vi.fn(),
}));

const review = {
  id: "generation", status: "pending_review", revision: 1, unit_total: 0, units: [],
  metadata: { confirmed: { doc_type: "内部材料" } }, quality_report: {},
};

async function loadReview() {
  render(<ReviewPanel />);
  fireEvent.change(screen.getByPlaceholderText("文档 ID"), { target: { value: "doc" } });
  fireEvent.change(screen.getByPlaceholderText("解析代次 ID"), { target: { value: "generation" } });
  fireEvent.click(screen.getByRole("button", { name: "载入待审代次" }));
  await screen.findByRole("button", { name: "发布" });
}

describe("generation publishing feedback", () => {
  afterEach(cleanup);
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchGenerationReview).mockResolvedValue(review as Awaited<ReturnType<typeof fetchGenerationReview>>);
  });

  it("shows a rejected publication without losing the review", async () => {
    vi.mocked(publishGeneration).mockRejectedValue(new Error("metadata_not_confirmed"));
    await loadReview();
    fireEvent.click(screen.getByRole("button", { name: "发布" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("metadata_not_confirmed");
    expect(screen.getByRole("button", { name: "发布" })).toBeEnabled();
  });

  it("reloads the published state and prevents another publication click", async () => {
    const published = { ...review, status: "published" } as Awaited<ReturnType<typeof fetchGenerationReview>>;
    vi.mocked(publishGeneration).mockResolvedValue(published);
    await loadReview();
    vi.mocked(fetchGenerationReview).mockResolvedValue(published);
    fireEvent.click(screen.getByRole("button", { name: "发布" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "已发布" })).toBeDisabled());
    expect(publishGeneration).toHaveBeenCalledWith("doc", "generation");
    expect(fetchGenerationReview).toHaveBeenCalledTimes(2);
  });
});
