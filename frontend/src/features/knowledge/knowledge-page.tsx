"use client";
import { useState } from "react";
import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { SearchPanel } from "./search-panel";
import { AnswerPanel } from "./answer-panel";
import { ReviewPanel } from "./review-panel";

export default function KnowledgePage() {
  const [tab, setTab] = useState<"search" | "answer" | "review">("search");
  const inspector = (
    <>
      <h2 className="inspector-title">知识库</h2>
      <div className="inspector-block">
        <p className="eyebrow">检索边界</p>
        <p className="empty-note">仅检索已发布代次；范围、版本与权限过滤发生在数据库内。重排失败会明确标注，不会伪装成“没有答案”。</p>
      </div>
    </>
  );
  return (
    <AppShell inspector={inspector}>
      <div className="page knowledge-page">
        <header className="page-header">
          <div>
            <span className="eyebrow">KNOWLEDGE BASE / RETRIEVAL &amp; QA</span>
            <h1>统一检索与单轮资料问答。</h1>
            <p>三路召回 + 语义重排 + 同文档配额；回答只依据本次证据并逐段引用。</p>
          </div>
        </header>
        <div className="document-toolbar">
          <Link href="/knowledge/ledger">事实台账、Wiki 与资料归档</Link>
          <Link href="/knowledge/health">健康与缺口看板</Link>
          {(["search", "answer", "review"] as const).map((item) => (
            <button key={item} className={tab === item ? "primary" : ""} onClick={() => setTab(item)}>
              {item === "search" ? "独立检索" : item === "answer" ? "资料问答" : "审核工作台"}
            </button>
          ))}
        </div>
        {tab === "search" && <SearchPanel />}
        {tab === "answer" && <AnswerPanel />}
        {tab === "review" && <ReviewPanel />}
      </div>
    </AppShell>
  );
}

