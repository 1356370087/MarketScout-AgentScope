import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MarkdownReport } from "./report-presentation";

afterEach(cleanup);

it("opens a known citation and preserves modifier-click navigation", () => {
  const source = { source_id: "s1", title: "白皮书", url: "https://example.com/report" };
  const select = vi.fn();
  render(<MarkdownReport value="[原文](https://example.com/report)" sources={[source]} selectedSource="s1" onSource={select} />);
  const link = screen.getByRole("link", { name: "原文" });
  expect(link).toHaveAttribute("data-selected", "true");
  expect(fireEvent.click(link)).toBe(false);
  expect(select).toHaveBeenCalledWith(source);
  select.mockClear();
  fireEvent.click(link, { ctrlKey: true });
  expect(select).not.toHaveBeenCalled();
  expect(link).toHaveAttribute("href", source.url);
});

it("keeps unmatched external links and removes unsafe destinations", () => {
  render(<MarkdownReport value="[外部](https://example.com/other) [不安全](javascript:alert)" sources={[]} />);
  expect(screen.getByRole("link", { name: "外部" })).toHaveAttribute("target", "_blank");
  expect(screen.queryByRole("link", { name: "不安全" })).not.toBeInTheDocument();
});

it("keeps the focused citation mounted when source selection or callbacks update", () => {
  const source = { source_id: "s1", url: "https://example.com/report" };
  const { rerender } = render(<MarkdownReport value="[原文](https://example.com/report)" sources={[source]} onSource={vi.fn()} />);
  const link = screen.getByRole("link", { name: "原文" });
  link.focus();
  rerender(<MarkdownReport value="[原文](https://example.com/report)" sources={[source]} selectedSource="s1" onSource={vi.fn()} />);
  expect(screen.getByRole("link", { name: "原文" })).toBe(link);
  expect(link).toHaveFocus();
});
