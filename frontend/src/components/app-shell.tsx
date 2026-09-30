"use client";

import { useQuery } from "@tanstack/react-query";
import { BarChart3, ChevronRight, CircleDot, Files, Languages, Library, LogOut, Menu, PanelRight, Radar, Settings, Shield, SquarePen, UserRound } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { ThemeToggle } from "@/components/theme-toggle";
import { SurfaceDialog } from "@/components/ui/workspace";
import { researchApi } from "@/lib/api";
import { authFetch, iamApi, localAuthBypass } from "@/lib/auth";

const statusLabel: Record<string, string> = { pending: "排队", running: "运行中", awaiting_clarification: "待澄清", awaiting_plan_approval: "待审批", awaiting_outline_approval: "待审批", awaiting_fetch_budget_approval: "待增额", completed: "已完成", failed: "失败", cancelled: "已取消" };

export function AppShell({ children, inspector }: { children: ReactNode; inspector?: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => researchApi.listRuns() });
  const { data: identity } = useQuery({ queryKey: ["identity"], queryFn: iamApi.me, enabled: !localAuthBypass, retry: false });
  const isAdmin = identity?.permissions.includes("iam.users.read");
  const displayName = identity?.display_name || identity?.email || (localAuthBypass ? "本地研究员" : "正在验证身份");
  const identityInitial = displayName.trim().slice(0, 1).toUpperCase() || "I";
  const logout = async () => { await authFetch("/api/auth/logout", { method: "POST" }); router.replace("/login"); router.refresh(); };
  const switchLocale = () => { const next = document.documentElement.lang === "en" ? "zh-CN" : "en"; document.cookie = `odr.locale=${next};path=/;max-age=31536000;samesite=lax`; location.reload(); };
  const link = (href: string, label: string, icon: ReactNode, active: boolean) => <Link href={href} aria-current={active ? "page" : undefined} className={active ? "active" : ""} onClick={() => setLeftOpen(false)}>{icon}<span>{label}</span></Link>;
  const sidebar = <>
    <Link className="brand-lockup" href="/research/new" aria-label="InsightForge 首页"><div className="brand-mark"><Radar size={19} /></div><strong>InsightForge</strong></Link>
    <Link className="new-run-button" href="/research/new" onClick={() => setLeftOpen(false)}><SquarePen size={17} /> 新建研究</Link>
    <nav className="shell-nav" aria-label="工作台功能">
      {link("/documents", "资料库", <Files size={17} />, pathname.startsWith("/documents"))}
      {link("/knowledge", "知识空间", <Library size={17} />, pathname.startsWith("/knowledge"))}
      {link("/usage", "用量与成本", <BarChart3 size={17} />, pathname === "/usage")}
    </nav>
    <section className="shell-history"><div className="sidebar-section-title"><span>最近研究</span><span>{runs.data?.items.length ?? ""}</span></div>
      <nav className="run-list" aria-label="研究历史">{runs.data?.items.map((item) => { const id = String(item.run_id); const active = pathname === `/research/${id}`; return <Link key={id} className={`run-item ${active ? "active" : ""}`} aria-current={active ? "page" : undefined} href={`/research/${id}`} onClick={() => setLeftOpen(false)}><i data-status={String(item.status)} /><span><b>{String(item.title ?? id)}</b><small>{statusLabel[String(item.status)] ?? String(item.status)}</small></span><ChevronRight size={14} /></Link>; })}</nav>
      {runs.isPending && <p className="empty-note">正在读取研究记录…</p>}{runs.isError && <p className="empty-note">历史暂不可用。<button className="ui-text-button" onClick={() => void runs.refetch()}>重试</button></p>}{runs.isSuccess && !runs.data.items.length && <p className="empty-note">提出第一个问题，研究会保存在这里。</p>}
    </section>
    <nav className="shell-nav shell-nav-bottom" aria-label="账户与偏好">
      {link("/settings", "研究设置", <Settings size={17} />, pathname === "/settings")}
      {link("/account/security", "账户安全", <UserRound size={17} />, pathname.startsWith("/account"))}
      {isAdmin && link("/admin", "管理后台", <Shield size={17} />, pathname === "/admin")}
    </nav>
    <div className="sidebar-preferences"><button className="sidebar-action" onClick={switchLocale}><Languages size={17} /> 中 / EN</button></div>
    {localAuthBypass && <div className="dev-badge"><CircleDot size={12} /> 本地开发 · 认证已跳过</div>}
    <div className="sidebar-identity"><span className="identity-avatar" aria-hidden="true">{identityInitial}</span><span><b>{displayName}</b><small>{identity?.roles.join(" · ") || (localAuthBypass ? "本地工作区" : "身份验证中")}</small></span>{!localAuthBypass && <button onClick={logout} aria-label="退出登录"><LogOut size={16} /></button>}</div>
  </>;
  return <div className={`workspace-shell ${inspector ? "with-inspector" : ""}`}>
    <a className="skip-link" href="#workspace-content">跳转到主要内容</a>
    <aside className="shell-sidebar" aria-label="主导航">{sidebar}</aside>
    <main className="workbench" id="workspace-content"><header className="shell-topbar"><button className="ui-icon shell-menu" onClick={() => { setRightOpen(false); setLeftOpen(true); }} aria-label="打开导航"><Menu size={19} /></button><span>InsightForge <span className="shell-top-caption">/ 研究工作台</span></span><div className="shell-top-actions"><ThemeToggle compact />{inspector && <button className="ui-icon shell-inspector-trigger" onClick={() => { setLeftOpen(false); setRightOpen(true); }} aria-label="打开研究信息"><PanelRight size={18} /></button>}</div></header>{children}</main>
    {inspector && <aside className="shell-inspector" aria-label="研究信息">{inspector}</aside>}
    <SurfaceDialog title="导航" open={leftOpen} onOpenChange={setLeftOpen} drawer><div className="shell-drawer-nav">{sidebar}</div></SurfaceDialog>
    {inspector && <SurfaceDialog title="研究信息" open={rightOpen} onOpenChange={setRightOpen} drawer>{inspector}</SurfaceDialog>}
  </div>;
}
