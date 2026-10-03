import { FileText, ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { Disclosure } from "@/components/ui/insight";
import type { KnowledgeEvidence } from "@/lib/contracts/knowledge";
import "./knowledge-ui.css";

export function KnowledgeEvidenceCard({ item, selected = false, markers = [] }: { item: KnowledgeEvidence; selected?: boolean; markers?: string[] }) {
  return <article id={`knowledge-evidence-${item.segment_id}`} tabIndex={-1} className="knowledge-evidence-card" data-selected={selected}>
    <header><span className="knowledge-file-icon"><FileText size={19} aria-hidden /></span><div><h3>{item.filename}</h3><span>{markers.length ? `引用 ${markers.join("、")}` : "检索证据"}</span></div>{item.relevance != null && <span className="knowledge-relevance">相关性 {item.relevance}/3</span>}</header>
    <p className="knowledge-excerpt">{item.text}</p>
    {(item.context_before || item.context_after) && <Disclosure title="查看前后文"><div className="knowledge-context">{item.context_before && <section><h4>前文</h4><p>{item.context_before}</p></section>}{item.context_after && <section><h4>后文</h4><p>{item.context_after}</p></section>}</div></Disclosure>}
    <footer><Link href={`/documents/${encodeURIComponent(item.document_id)}`}>查看资料 <ArrowUpRight size={14} /></Link><Link href={`/knowledge?review=1&document_id=${encodeURIComponent(item.document_id)}&generation_id=${encodeURIComponent(item.generation_id)}`}>核验此代次</Link></footer>
  </article>;
}
