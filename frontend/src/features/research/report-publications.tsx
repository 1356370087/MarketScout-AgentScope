"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  Braces,
  Download,
  FileText,
  LayoutTemplate,
  LoaderCircle,
  Palette,
  Presentation,
  RefreshCw,
  RotateCcw,
} from "lucide-react";
import { useMemo, useState } from "react";
import { usePublications } from "@/hooks/use-publications";
import { researchApi } from "@/lib/api";
import {
  applyPublicationThemePreset,
  loadPublicationTheme,
  sanitizePublicationTheme,
} from "@/lib/settings";
import type { PublicationFormat, PublicationJob, PublicationTheme } from "@/lib/types";

const formats: Array<{
  format: Exclude<PublicationFormat, "markdown">;
  label: string;
  icon: typeof FileText;
}> = [
  { format: "pdf", label: "PDF", icon: FileText },
  { format: "docx", label: "Word", icon: FileText },
  { format: "pptx", label: "PowerPoint", icon: Presentation },
  { format: "json", label: "JSON", icon: Braces },
  { format: "one_pager", label: "One Pager", icon: LayoutTemplate },
];

const statusLabels: Record<string, string> = {
  queued: "排队中",
  running: "生成中",
  completed: "可下载",
  failed: "生成失败",
};

type PublicationSlot = {
  latest?: PublicationJob;
  previousCompleted?: PublicationJob;
};

function hasDownload(job?: PublicationJob): boolean {
  return Boolean(job?.download_url || job?.artifact?.download_url);
}

function latestByFormat(items: PublicationJob[]) {
  // The API currently returns newest-first, but sorting here keeps the UI
  // correct for older stores and makes the fallback selection deterministic.
  const newestFirst = [...items].sort(
    (left, right) => Number(right.created_at) - Number(left.created_at),
  );
  return Object.fromEntries(
    formats.map(({ format }) => {
      const matching = newestFirst.filter((item) => item.format === format);
      const latest = matching[0];
      const previousCompleted = matching.find(
        (item) => item.status === "completed" && item.publication_id !== latest?.publication_id && hasDownload(item),
      );
      return [format, { latest, previousCompleted } satisfies PublicationSlot];
    }),
  ) as Partial<Record<PublicationFormat, PublicationSlot>>;
}

function publicationDownloadHref(runId: string, job: PublicationJob): string {
  return researchApi.publicationDownloadUrl(
    runId,
    job.publication_id,
    job.download_url ?? job.artifact?.download_url,
  );
}

type ReportPublicationsProps = {
  runId: string;
  initial?: PublicationJob[];
  preferredFormat?: string | null;
  defaultTheme?: PublicationTheme;
};

function ReportPublicationsView({
  runId,
  initial = [],
  preferredFormat,
  defaultTheme,
}: ReportPublicationsProps) {
  const queryClient = useQueryClient();
  const publications = usePublications(runId, initial);
  const [draftTheme, setDraftTheme] = useState<PublicationTheme>(
    () => defaultTheme ?? initial[0]?.theme ?? loadPublicationTheme(),
  );
  const [themeDirty, setThemeDirty] = useState(false);
  const [error, setError] = useState("");
  const slots = useMemo(
    () => latestByFormat(publications.data?.items ?? []),
    [publications.data?.items],
  );
  const persistedTheme = publications.data?.default_theme ?? defaultTheme ?? initial[0]?.theme;
  const theme = themeDirty ? draftTheme : persistedTheme ?? draftTheme;

  const updateTheme = (next: PublicationTheme) => {
    setDraftTheme(next);
    setThemeDirty(true);
  };

  const create = useMutation({
    mutationFn: (format: Exclude<PublicationFormat, "markdown">) =>
      researchApi.createPublication(
        runId,
        format,
        themeDirty ? sanitizePublicationTheme(theme) : undefined,
      ),
    onMutate: () => setError(""),
    onSuccess: async () =>
      queryClient.invalidateQueries({ queryKey: ["publications", runId] }),
    onError: (cause) =>
      setError(cause instanceof Error ? cause.message : "发布任务创建失败。"),
  });
  const retry = useMutation({
    mutationFn: (publicationId: string) =>
      researchApi.retryPublication(runId, publicationId),
    onMutate: () => setError(""),
    onSuccess: async () =>
      queryClient.invalidateQueries({ queryKey: ["publications", runId] }),
    onError: (cause) =>
      setError(cause instanceof Error ? cause.message : "重试失败。"),
  });

  const normalizedPreferred =
    preferredFormat === "slides"
      ? "pptx"
      : preferredFormat === "structured_json"
        ? "json"
        : preferredFormat;

  return (
    <section className="publication-panel" aria-label="报告发布">
      <header>
        <div>
          <span className="eyebrow">PUBLISH / OWNER ONLY</span>
          <h2>交付文件</h2>
        </div>
        <span
          className={`status-chip ${publications.data?.worker === "ready" ? "" : "warning"}`}
          data-status={publications.data?.worker === "ready" ? "completed" : "running"}
        >
          {publications.data?.worker === "ready" ? "WORKER READY" : "WORKER WAITING"}
        </span>
      </header>
      <div className="publication-format-grid">
        {formats.map(({ format, label, icon: Icon }) => {
          const slot = slots[format];
          const job = slot?.latest;
          const previousCompleted = slot?.previousCompleted;
          const busy = job?.status === "queued" || job?.status === "running";
          const downloadable = job?.status === "completed" && hasDownload(job);
          return (
            <div
              className={`publication-format ${normalizedPreferred === format ? "preferred" : ""}`}
              key={format}
            >
              <div>
                <Icon size={17} />
                <span>
                  <b>{label}</b>
                  <small>
                    {job ? statusLabels[job.status] ?? job.status : "尚未生成"}
                    {job?.artifact?.size_bytes
                      ? ` · ${(job.artifact.size_bytes / 1024 / 1024).toFixed(1)} MB`
                      : ""}
                  </small>
                </span>
              </div>
              <div className="publication-format-actions">
                {downloadable ? (
                  <a
                    className="icon-action"
                    title={`下载 ${label}`}
                    href={publicationDownloadHref(runId, job)}
                    download
                  >
                    <Download size={15} />
                  </a>
                ) : job?.status === "failed" && job.retryable ? (
                  <button
                    type="button"
                    className="icon-action"
                    title="重试生成"
                    disabled={retry.isPending}
                    onClick={() => retry.mutate(job.publication_id)}
                  >
                    <RefreshCw size={15} />
                  </button>
                ) : job?.status === "failed" ? (
                  <button
                    type="button"
                    className="icon-action secondary-icon"
                    title="该任务不可重试"
                    disabled
                  >
                    <Ban size={15} />
                  </button>
                ) : job?.status === "completed" ? (
                  <button
                    type="button"
                    className="icon-action secondary-icon"
                    title="文件不可用"
                    disabled
                  >
                    <Ban size={15} />
                  </button>
                ) : (
                  <button
                    type="button"
                    className="icon-action"
                    title={`生成 ${label}`}
                    disabled={busy || create.isPending}
                    onClick={() => create.mutate(format)}
                  >
                    {busy ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : (
                      <Download size={15} />
                    )}
                  </button>
                )}
                {job?.status === "completed" && (
                  <button
                    type="button"
                    className="icon-action secondary-icon"
                    title="用当前主题生成版本"
                    disabled={create.isPending}
                    onClick={() => create.mutate(format)}
                  >
                    <RotateCcw size={14} />
                  </button>
                )}
                {previousCompleted && !downloadable && (
                  <a
                    className="icon-action secondary-icon"
                    title={`下载上一版 ${label}`}
                    href={publicationDownloadHref(runId, previousCompleted)}
                    download
                  >
                    <Download size={14} />
                  </a>
                )}
              </div>
              {job?.status === "failed" && (
                <small className="publication-error-code">
                  {job.error_code ?? "publication_failed"}
                </small>
              )}
            </div>
          );
        })}
      </div>
      {error && <p className="source-error" role="alert">{error}</p>}
      <details className="publication-theme">
        <summary><Palette size={14} /> 发布主题</summary>
        <div className="publication-theme-grid">
          <label>
            预设
            <select
              value={theme.preset}
              onChange={(event) =>
                updateTheme(
                  applyPublicationThemePreset(
                    theme,
                    event.target.value as PublicationTheme["preset"],
                  ),
                )
              }
            >
              <option value="default">Default</option>
              <option value="boardroom">Boardroom</option>
              <option value="academic">Academic</option>
            </select>
          </label>
          <label>
            主色
            <input
              type="color"
              value={theme.primary_color}
              onChange={(event) =>
                updateTheme({ ...theme, primary_color: event.target.value.toUpperCase() })
              }
            />
          </label>
          <label>
            强调色
            <input
              type="color"
              value={theme.accent_color}
              onChange={(event) =>
                updateTheme({ ...theme, accent_color: event.target.value.toUpperCase() })
              }
            />
          </label>
          <label>
            字体
            <select
              value={theme.font_family}
              onChange={(event) =>
                updateTheme({
                  ...theme,
                  font_family: event.target.value as PublicationTheme["font_family"],
                })
              }
            >
              <option value="cjk_sans">CJK Sans</option>
              <option value="sans">Sans</option>
              <option value="serif">Serif</option>
            </select>
          </label>
          <label>
            标签语言
            <select
              value={theme.locale}
              onChange={(event) =>
                updateTheme({
                  ...theme,
                  locale: event.target.value as PublicationTheme["locale"],
                })
              }
            >
              <option value="zh-CN">中文</option>
              <option value="en-US">English</option>
            </select>
          </label>
          <label>
            页面
            <select
              value={theme.pdf_page_size}
              onChange={(event) =>
                updateTheme({
                  ...theme,
                  pdf_page_size: event.target.value as PublicationTheme["pdf_page_size"],
                })
              }
            >
              <option value="a4">A4</option>
              <option value="letter">Letter</option>
            </select>
          </label>
          <label>
            幻灯片
            <select
              value={theme.pptx_aspect_ratio}
              onChange={(event) =>
                updateTheme({
                  ...theme,
                  pptx_aspect_ratio: event.target.value as PublicationTheme["pptx_aspect_ratio"],
                })
              }
            >
              <option value="16:9">16:9</option>
              <option value="4:3">4:3</option>
            </select>
          </label>
          <label className="theme-footer">
            页脚
            <input
              maxLength={120}
              value={theme.footer_text}
              onChange={(event) => updateTheme({ ...theme, footer_text: event.target.value })}
            />
          </label>
        </div>
      </details>
    </section>
  );
}

/** Remount the stateful panel when the user switches to another Run. */
export function ReportPublications(props: ReportPublicationsProps) {
  return <ReportPublicationsView key={props.runId} {...props} />;
}
