"use client";
import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { AppShell } from "@/components/app-shell";
import { PageHeading, Tabs } from "@/components/ui/workspace";
import { SearchPanel } from "./search-panel";
import { AnswerPanel } from "./answer-panel";
import { ReviewPanel } from "./review-panel";
import { KnowledgeNav } from "./knowledge-nav";

export default function KnowledgePage() {
  const params = useSearchParams();
  const [selectedTab, setTab] = useState<string>();
  const tab = selectedTab ?? (params.get("review") === "1" ? "review" : "search");
  return <AppShell><div className="page knowledge-page"><PageHeading title="组织的知识，触手可及" description="在已发布资料中检索、提问与核验，让每个答案保留依据。" /><KnowledgeNav /><Tabs label="知识工具" value={tab} onChange={setTab} items={[{ value: "search", label: "资料检索" }, { value: "answer", label: "资料问答" }, { value: "review", label: "审核工作台" }]}>{tab === "search" && <SearchPanel />}{tab === "answer" && <AnswerPanel />}{tab === "review" && <ReviewPanel key={`${params.get("document_id")}:${params.get("generation_id")}`} initialDocumentId={params.get("document_id") ?? ""} initialGenerationId={params.get("generation_id") ?? ""} />}</Tabs></div></AppShell>;
}
