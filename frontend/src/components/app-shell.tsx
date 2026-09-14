"use client";

import { useQuery } from "@tanstack/react-query";
import {
  BarChart3,
  ChevronRight,
  CircleDot,
  Files,
  Languages,
  LogOut,
  Menu,
  PanelRight,
  Plus,
  Radar,
  Settings,
  Shield,
  UserRound,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { ThemeToggle } from "@/components/theme-toggle";
import { researchApi } from "@/lib/api";
import { authFetch, iamApi, localAuthBypass } from "@/lib/auth";

const statusLabel: Record<string, string> = {
  pending: "排队",
  running: "运行中",
  awaiting_clarification: "待澄清",
  awaiting_plan_approval: "待审批",
  awaiting_outline_approval: "待审批",
  awaiting_fetch_budget_approval: "待增额",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function AppShell({ children, inspector }: { children: ReactNode; inspector?: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const { data } = useQuery({ queryKey: ["runs"], queryFn: () => researchApi.listRuns() });
  const { data: identity } = useQuery({ queryKey: ["identity"], queryFn: iamApi.me, enabled: !localAuthBypass, retry: false });
  const isAdmin = identity?.permissions.includes("iam.users.read");
  const displayName = identity?.display_name || identity?.email || (localAuthBypass ? "本地研究员" : "正在验证身份");
  const identityInitial = displayName.trim().slice(0, 1).toUpperCase() || "I";

  const logout = async () => {
    await authFetch("/api/auth/logout", { method: "POST" });
    router.replace("/login");
    router.refresh();
  };
  const switchLocale = () => {
    const next = document.documentElement.lang === "en" ? "zh-CN" : "en";
    document.cookie = `odr.locale=${next};path=/;max-age=31536000;samesite=lax`;
    location.reload();
  };

  const sidebar = <>
    <div className="sidebar-head">
      <Link className="brand-lockup" href="/research/new" aria-label="InsightForge 首页">
        <div className="brand-mark"><Radar size={19} /></div>
        <div><strong>InsightForge</strong><span>企业竞争情报研究台</span></div>
      </Link>
    </div>
    {localAuthBypass && <div className="dev-badge"><CircleDot size={12} /> 本地开发 · 认证已跳过</div>}
    <Link className="new-run-button" href="/research/new"><Plus size={17} /> 发起竞品研究</Link>
    <section className="sidebar-history">
      <div className="sidebar-section-title"><span>最近研究</span><span>{data?.items.length ?? 0}</span></div>
      <nav className="run-list" aria-label="研究历史">
        {data?.items.map((item) => {
          const id = String(item.run_id);
          const active = pathname.endsWith(id);
          return <Link key={id} className={`run-item ${active ? "active" : ""}`} href={`/research/${id}`}>
            <i data-status={String(item.status)} />
            <span><b>{String(item.title ?? id)}</b><small>{statusLabel[String(item.status)] ?? String(item.status)}</small></span>
            <ChevronRight size={14} />
          </Link>;
        })}
        {!data?.items.length && <p className="empty-note">还没有研究记录。先提出一个竞争问题，研究过程与结论会保存在这里。</p>}
      </nav>
    </section>
    <nav className="sidebar-footer" aria-label="工作台功能">
      <Link className={pathname.startsWith("/documents") ? "active" : ""} href="/documents"><Files size={17} /> 企业资料库</Link>
      <Link className={pathname.startsWith("/knowledge/health") ? "active" : ""} href="/knowledge/health"><Files size={17} /> 知识库健康</Link>
      <Link className={pathname === "/usage" ? "active" : ""} href="/usage"><BarChart3 size={17} /> 用量与成本</Link>
      <Link className={pathname === "/settings" ? "active" : ""} href="/settings"><Settings size={17} /> 研究配置</Link>
      <Link className={pathname.startsWith("/account") ? "active" : ""} href="/account/security"><UserRound size={17} /> 账户安全</Link>
      {isAdmin && <Link className={pathname === "/admin" ? "active" : ""} href="/admin"><Shield size={17} /> 身份管理</Link>}
    </nav>
    <div className="sidebar-preferences">
      <ThemeToggle />
      <button className="sidebar-action" onClick={switchLocale}><Languages size={17} /> 中 / EN</button>
    </div>
    <div className="sidebar-identity">
      <span className="identity-avatar" aria-hidden="true">{identityInitial}</span>
      <span><b>{displayName}</b><small>{identity?.roles.join(" · ") || (localAuthBypass ? "developer" : "IAM / CHECKING")}</small></span>
      {!localAuthBypass && <button onClick={logout} title="退出登录" aria-label="退出登录"><LogOut size={16} /></button>}
    </div>
  </>;

  return <div className={`command-shell ${inspector ? "has-inspector" : ""}`}>
    <aside className={`left-rail ${leftOpen ? "drawer-open" : ""}`}>
      {sidebar}
      <button className="drawer-close" onClick={() => setLeftOpen(false)} aria-label="关闭导航"><X /></button>
    </aside>
    <main className="workbench">
      <header className="mobile-bar">
        <button onClick={() => setLeftOpen(true)} aria-label="打开导航"><Menu /></button>
        <Link className="mobile-brand" href="/research/new"><Radar size={16} /><span>InsightForge</span></Link>
        <div className="mobile-actions"><ThemeToggle compact />{inspector && <button onClick={() => setRightOpen(true)} aria-label="打开研究信息"><PanelRight /></button>}</div>
      </header>
      {children}
    </main>
    {inspector && <aside className={`right-rail ${rightOpen ? "drawer-open" : ""}`}>
      {inspector}
      <button className="drawer-close" onClick={() => setRightOpen(false)} aria-label="关闭信息栏"><X /></button>
    </aside>}
    {(leftOpen || rightOpen) && <button className="drawer-scrim" onClick={() => { setLeftOpen(false); setRightOpen(false); }} aria-label="关闭抽屉" />}
  </div>;
}
