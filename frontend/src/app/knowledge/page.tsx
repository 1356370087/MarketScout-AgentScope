import { Suspense } from "react";
import KnowledgePage from "@/features/knowledge/knowledge-page";

export default function Page() {
  return <Suspense fallback={<p>正在读取知识空间…</p>}><KnowledgePage /></Suspense>;
}
