"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { platformApi } from "@/lib/platform";
import { Heading, Alert } from "./ui";
export default function ToolsPage() {
  const status = useQuery({ queryKey: ["platform", "status"], queryFn: platformApi.status });
  const id = status.data?.active_revision;
  const config = useQuery({ queryKey: ["platform", "configuration", id], queryFn: () => platformApi.configuration(id!), enabled: !!id });
  return <><Heading title="工具与安全策略" description="工具接入、搜索与出网策略跟随平台配置版本发布。" /><Alert error={status.error || config.error} />{!id ? <Alert>平台尚未发布配置。<Link href="/admin/configurations">进入配置中心</Link></Alert> : <><div className="platform-actions"><Link className="primary" href={`/admin/configurations/${id}`}>查看当前配置并复制草稿</Link></div><div className="platform-grid">{[["搜索渠道", config.data?.document.runtime.search_providers ?? config.data?.document.runtime.search_api], ["MCP 接入", config.data?.document.runtime.mcp_config], ["浏览器工具", config.data?.document.runtime.browser_mcp_config], ["沙箱策略", config.data?.document.sandbox]].map(([label, value]) => <section className="panel platform-panel" key={String(label)}><h2>{String(label)}</h2><pre className="platform-json">{JSON.stringify(value ?? "未配置", null, 2)}</pre></section>)}</div></>}</>;
}
