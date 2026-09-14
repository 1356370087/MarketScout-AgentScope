import { AppShell } from "@/components/app-shell";
import { ResearchComposer } from "@/components/research-composer";

function NewInspector() {
  return <>
    <span className="inspector-kicker">ANALYSIS BRIEF</span>
    <h2 className="inspector-title">一次完整的竞品研究</h2>
    <div className="inspector-block"><div className="summary-grid"><div><b>06</b><span>研究阶段</span></div><div><b>SSE</b><span>实时证据流</span></div></div></div>
    <div className="inspector-block"><p className="eyebrow">建议输入</p><p className="empty-note">明确比较对象、决策背景、时间范围和判断标准。问题越具体，结论越适合直接进入决策材料。</p></div>
    <div className="inspector-block"><p className="eyebrow">可信边界</p><p className="empty-note">公开网络、内部资料和指定来源都保留引用路径；系统会标记推断、冲突与证据缺口。</p></div>
  </>;
}

export default function NewResearchPage() {
  return <AppShell inspector={<NewInspector />}><div className="page hero-console">
    <div className="hero-context"><span>深度研究</span><i />企业竞品分析</div>
    <h1 className="hero-title">把一个竞争问题，<br /><em>变成可行动的判断。</em></h1>
    <p className="hero-copy">从公开市场信号与企业资料出发，并行验证产品、定价、渠道与战略动向，交付带来源、可复核的竞争情报。</p>
    <ResearchComposer />
    <div className="config-strip" aria-label="研究能力"><span>并行任务拆解</span><span>证据去重与引用</span><span>质量门禁</span><span>人工审批可恢复</span></div>
  </div></AppShell>;
}
