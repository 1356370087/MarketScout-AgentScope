import { AppShell } from "@/components/app-shell";
import { ResearchComposer } from "@/features/research/research-composer";

export default function NewResearchPage() {
  return <AppShell><div className="page hero-console"><span className="eyebrow">从好奇出发，让判断有据可循</span><h1 className="hero-title">今天，想深入了解什么？</h1><p className="hero-copy">把一个竞争问题，变成可行动的判断。研究、证据与结论，在这里自然连接。</p><ResearchComposer /><div className="config-strip" aria-label="研究能力"><span>并行研究</span><span>来源可追溯</span><span>人工确认</span></div></div></AppShell>;
}
