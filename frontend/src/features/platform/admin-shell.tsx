"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Coins, Fingerprint, LayoutDashboard, Menu, Settings2, ShieldCheck, Users, Waypoints, X } from "lucide-react";
import { iamApi, localAuthBypass } from "@/lib/auth";
import { canOpenAdmin } from "@/lib/platform";
import { WorkspaceSwitch } from "@/components/workspace-switch";
import { ThemeToggle } from "@/components/theme-toggle";
import { LoadingSkeleton } from "@/components/ui/insight";
import "./platform.css";
import { PlatformModal } from "./ui";

type Guard = { dirty: boolean; setDirty: (value: boolean) => void; registerSave: (save: (() => Promise<void>) | null) => void };
const DraftGuard = createContext<Guard>({ dirty: false, setDirty: () => {}, registerSave: () => {} });
export const useDraftGuard = () => useContext(DraftGuard);

const navigation = [
  { href: "/admin", label: "平台概览", icon: LayoutDashboard, permissions: ["platform.operations.read"] },
  { href: "/admin/configurations", label: "配置中心", icon: Settings2, permissions: ["platform.config.read", "platform.config.personal"] },
  { href: "/admin/models", label: "模型与凭据", icon: Waypoints, permissions: ["platform.credentials.manage", "platform.credentials.personal"] },
  { href: "/admin/tools", label: "工具与安全策略", icon: ShieldCheck, permissions: ["platform.config.read"] },
  { href: "/admin/operations", label: "任务与 Worker", icon: Activity, permissions: ["platform.operations.read"] },
  { href: "/admin/usage", label: "用量与配额", icon: Coins, permissions: ["platform.usage.read", "platform.quotas.write"] },
  { href: "/admin/members", label: "成员与权限", icon: Users, permissions: ["iam.users.read", "iam.roles.read"] },
  { href: "/admin/audit", label: "审计记录", icon: Fingerprint, permissions: ["platform.audit.read", "iam.audit.read"] },
];

export function AdminShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const identity = useQuery({ queryKey: ["identity"], queryFn: iamApi.me, enabled: !localAuthBypass, retry: false });
  const [menu, setMenu] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [pendingPath, setPendingPath] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const saveRef = useRef<(() => Promise<void>) | null>(null);
  const registerSave = useCallback((save: (() => Promise<void>) | null) => { saveRef.current = save; }, []);
  useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
  if (localAuthBypass) return <main className="platform-access"><h1>使用真实账号进入管理后台</h1><p>请关闭本地认证旁路，并使用管理员或开发者账号登录。</p><Link href="/research/new">返回研究工作台</Link></main>;
  if (identity.isPending) return <LoadingSkeleton label="正在验证后台访问权限…" />;
  if (!identity.data || !canOpenAdmin(identity.data.permissions)) return <main className="platform-access"><h1>没有后台访问权限</h1><Link href="/research/new">返回研究工作台</Link></main>;
  const permitted = navigation.filter((item) => item.permissions.some((permission) => identity.data.permissions.includes(permission)));
  const current = navigation.find((item) => item.href === pathname || (item.href !== "/admin" && pathname.startsWith(item.href + "/")));
  const pageAllowed = !current || permitted.includes(current) || pathname === "/admin";
  function leave() { if (pendingPath) { setDirty(false); setPendingPath(null); setMenu(false); router.push(pendingPath); } }
  async function saveAndLeave() {
    if (!saveRef.current) return;
    setSaving(true); setSaveError("");
    try { await saveRef.current(); leave(); } catch (error) { setSaveError(error instanceof Error ? error.message : "保存失败"); } finally { setSaving(false); }
  }
  return <DraftGuard.Provider value={{ dirty, setDirty, registerSave }}><div className="platform-layout" onClickCapture={(event) => {
    const anchor = (event.target as HTMLElement).closest("a[href]");
    const href = anchor?.getAttribute("href");
    if (dirty && href?.startsWith("/") && href !== pathname) { event.preventDefault(); event.stopPropagation(); setPendingPath(href); }
  }}>
    <a className="skip-link" href="#platform-main">跳转到后台内容</a>
    <aside className={`platform-sidebar ${menu ? "open" : ""}`}><Link className="platform-brand" href="/admin"><ShieldCheck size={25} /><span>InsightForge<small>平台管理</small></span></Link>
      <button type="button" className="platform-menu-close secondary" aria-label="关闭管理导航" onClick={() => setMenu(false)}><X size={16} /></button>
      <WorkspaceSwitch identity={identity.data} admin />
      <nav aria-label="管理后台">{permitted.map(({ href, label, icon: Icon }) => <Link key={href} href={href} onClick={() => setMenu(false)} aria-current={pathname === href || href !== "/admin" && pathname.startsWith(href + "/") ? "page" : undefined}><Icon size={17} />{label}</Link>)}</nav>
      <div className="platform-identity"><b>{identity.data.display_name || identity.data.email}</b><small>{identity.data.roles.join(" · ")}</small><ThemeToggle /></div>
    </aside>
    <div className="platform-content"><header className="platform-topbar"><button type="button" className="platform-menu secondary" aria-label="切换管理导航" onClick={() => setMenu(!menu)}><Menu size={18} /></button><span>管理后台 <span aria-hidden> / </span> {current?.label ?? "配置详情"}</span><span className="platform-context">{identity.data.permissions.includes("platform.config.publish") ? "平台管理员" : "个人调试空间"}</span></header>
      <main id="platform-main">{pageAllowed ? children : <div className="panel platform-panel"><h1>没有此模块的访问权限</h1><Link href={permitted[0]?.href ?? "/research/new"}>返回可用页面</Link></div>}</main>
    </div>
    {pendingPath && <PlatformModal title="配置草稿尚未保存" onClose={() => { setPendingPath(null); }}><p>保存草稿后可以继续编辑和发布。</p>{saveError && <p role="alert">{saveError}</p>}<div className="platform-actions"><button className="primary" disabled={saving} onClick={() => void saveAndLeave()}>保存并切换</button><button className="danger" disabled={saving} onClick={leave}>放弃修改</button><button className="secondary" onClick={() => setPendingPath(null)}>继续编辑</button></div></PlatformModal>}
  </div></DraftGuard.Provider>;
}
