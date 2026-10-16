"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { iamApi } from "@/lib/auth";
import { platformFetch, money, type PlatformState } from "@/lib/platform";
import { Heading, Alert, WorkerState } from "./ui";
import { LoadingSkeleton } from "@/components/ui/insight";

type Overview = { configuration: PlatformState; pending_releases: number; cost_micro_usd: number; reserved_micro_usd: number; run_count: number; workers: Record<string, unknown>; runtime_error: string | null };
export default function OverviewPage() {
  const identity = useQuery({ queryKey: ["identity"], queryFn: iamApi.me });
  const canRead = identity.data?.permissions.includes("platform.operations.read");
  const query = useQuery({ queryKey: ["platform", "overview"], queryFn: () => platformFetch<Overview>("/overview"), enabled: !!canRead, refetchInterval: 15000 });
  if (identity.data && !canRead) return <><Heading title="你的管理空间" description="配置和权限按当前账号开放。" /><div className="platform-grid">{identity.data.permissions.includes("platform.config.personal") && <section className="panel platform-panel"><h2>个人调试配置</h2><p>导入和复用自己的模型与研究配置。</p><Link href="/admin/configurations">进入配置中心</Link></section>}{identity.data.permissions.includes("iam.users.read") && <section className="panel platform-panel"><h2>成员与权限</h2><Link href="/admin/members">管理成员</Link></section>}</div></>;
  return <><Heading title="平台概览" description="查看配置、运行和成本，定位需要处理的事项。"><Link className="primary" href="/admin/configurations">进入配置中心</Link></Heading><Alert error={query.error} />{query.isPending ? <LoadingSkeleton label="正在读取平台状态…" /> : query.data && <>
    {!query.data.configuration.active_revision && <Alert><b>平台尚未完成首次配置。</b> 导入配置、填写接入与预算，然后确认发布。<Link href="/admin/configurations"> 开始配置</Link></Alert>}
    <div className="platform-metrics">{[["研究运行", query.data.run_count], ["累计结算", money(query.data.cost_micro_usd)], ["在途预留", money(query.data.reserved_micro_usd)], ["待应用版本", query.data.pending_releases]].map(([label, value]) => <div className="platform-metric" key={label}><small>{label}</small><strong>{value}</strong></div>)}</div>
    <div className="platform-grid"><section className="panel platform-panel"><h2>当前配置</h2><p>{query.data.configuration.active_revision ? `平台配置第 ${query.data.configuration.generation} 次生效` : "等待初始化"}</p>{query.data.configuration.active_revision && <Link href={`/admin/configurations/${query.data.configuration.active_revision}`}>查看生效版本</Link>}<Alert error={query.data.runtime_error} /></section><section className="panel platform-panel"><h2>后台服务</h2>{Object.entries(query.data.workers).map(([name, info]) => <WorkerState key={name} name={name === "documents" ? "文档摄取" : name === "knowledge" ? "知识库维护" : "报告发布"} value={info} />)}<Link href="/admin/operations">查看任务与 Worker</Link></section></div>
  </>}</>;
}
