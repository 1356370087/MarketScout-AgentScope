"use client";

import { ClipboardCheck } from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import type { ReportReviewSummary } from "@/lib/contracts/publications";

export function MarkdownReport({ value }: { value: string }) {
  return <ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]} components={{
    h1: ({ node, children }) => <h1 id={`report-line-${node?.position?.start.line}`}>{children}</h1>,
    h2: ({ node, children }) => <h2 id={`report-line-${node?.position?.start.line}`}>{children}</h2>,
    h3: ({ node, children }) => <h3 id={`report-line-${node?.position?.start.line}`}>{children}</h3>,
    a: ({ href, children }) => { const localDocument = Boolean(href && /^\/documents\/[0-9a-f-]+(?:\?chunk=[0-9a-f-]+)?$/i.test(href)); const safe = href?.startsWith("http://") || href?.startsWith("https://") || href?.startsWith("#") || localDocument; return safe ? <a href={href} target={href?.startsWith("http") ? "_blank" : undefined} rel="noopener noreferrer">{children}</a> : <span>{children}</span>; }
  }}>{value}</ReactMarkdown>;
}

function collectHeadings(value: string) {
  let fence = "";
  const headings = value.split("\n").flatMap((line, index) => {
    const marker = /^ {0,3}(`{3,}|~{3,})/.exec(line)?.[1];
    if (marker) { if (!fence) fence = marker; else if (marker[0] === fence[0] && marker.length >= fence.length) fence = ""; return []; }
    if (fence) return [];
    const heading = /^ {0,3}(#{1,3})\s+(.+?)(?:\s+#+)?\s*$/.exec(line);
    return heading ? [{ line: index + 1, level: heading[1].length, title: heading[2].replace(/[*_`]/g, "") }] : [];
  });
  return headings;
}

export function ReportContents({ value }: { value: string }) {
  const headings = collectHeadings(value);
  return headings.length ? <nav className="report-contents" aria-label="报告目录"><h3>报告目录</h3>{headings.map((heading) => <a key={heading.line} href={`#report-line-${heading.line}`} data-level={heading.level}>{heading.title}</a>)}</nav> : null;
}

const reportReviewStatusLabels: Record<string, string> = {
  pending: "等待复核", running: "复核中", revising: "修订中", revised: "已修订",
  passed: "复核通过", pass: "复核通过", completed: "已完成", failed: "复核失败",
  fail: "复核失败", degraded: "降级交付", skipped: "未执行", error: "复核错误",
};
const reportReviewDimensionLabels: Record<string, string> = {
  coverage: "覆盖", citation_correctness: "引用正确性", contradictions: "矛盾处理",
  unsupported_claims: "无依据主张", redundancy: "冗余控制", executive_readability: "高管可读性",
};

function reportReviewTone(status: string): "running" | "completed" | "failed" | "pending" | "degraded" {
  if (["running", "revising"].includes(status)) return "running";
  if (["passed", "pass", "completed", "revised"].includes(status)) return "completed";
  if (status === "degraded" || status === "skipped") return "degraded";
  if (["failed", "fail", "error"].includes(status)) return "failed";
  return "pending";
}

export function ReportReviewPanel({ review }: { review: ReportReviewSummary }) {
  const status = String(review.status ?? (review.decision === "pass" ? "passed" : "pending"));
  const dimensions = Object.entries(review.dimensions ?? {}).filter(([, value]) => typeof value === "number" && Number.isFinite(value));
  const issueCount = typeof review.issue_count === "number" ? review.issue_count : undefined;
  const criticalCount = typeof review.critical_issue_count === "number" ? review.critical_issue_count : undefined;
  return <section className="panel report-review-panel" data-testid="report-review-status" aria-live="polite">
    <div className="panel-header"><h2><ClipboardCheck size={15} /> 报告复核</h2><span className="status-chip" data-status={reportReviewTone(status)}>{reportReviewStatusLabels[status] ?? status}</span></div>
    <div className="panel-body">
      <div className="report-review-meta">
        {review.attempt !== undefined && <span>REVIEW {review.attempt}</span>}
        {review.revision_count !== undefined && <span>REVISION {review.revision_count}</span>}
        {issueCount !== undefined && <span>ISSUES {issueCount}</span>}
        {criticalCount !== undefined && <span>CRITICAL {criticalCount}</span>}
        {review.decision && <span>DECISION {String(review.decision).toUpperCase()}</span>}
      </div>
      {dimensions.length > 0 && <div className="report-review-dimensions">{dimensions.map(([key, value]) => <div key={key}><span>{reportReviewDimensionLabels[key] ?? key}</span><b>{Math.round(Math.max(0, Math.min(1, value as number)) * 100)}%</b></div>)}</div>}
      {(review.summary || review.reason) && <p className="report-review-note">{review.summary ?? review.reason}</p>}
    </div>
  </section>;
}

