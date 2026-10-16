"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { AuthShell } from "@/components/auth-shell";
import { authFetch } from "@/lib/auth";
import { PasswordField } from "@/features/iam/identity-components";
import "./platform.css";

export default function SetupPage() {
  const router = useRouter();
  const cache = useQueryClient();
  const status = useQuery({ queryKey: ["setup-status"], queryFn: () => authFetch<{ initialized: boolean; configured: boolean; setup_available: boolean }>("/api/setup/status"), retry: false });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function initialize(form: FormData) {
    setBusy(true); setError("");
    try {
      await authFetch("/api/setup/bootstrap", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(form)) });
      await authFetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: form.get("email"), password: form.get("password") }) });
      await cache.invalidateQueries(); router.replace("/admin/configurations"); router.refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "初始化失败"); } finally { setBusy(false); }
  }
  return <AuthShell eyebrow="首次启动" title="初始化 InsightForge" copy="创建首位管理员，然后导入配置、确认预算并发布。" footer={<Link href="/login">已有账号，前往登录</Link>}>
    {status.error && <p role="alert" className="form-alert error">无法读取初始化状态，请确认数据库已完成迁移并且 API 可访问。</p>}
    {status.data?.initialized ? <div className="platform-alert">管理员已创建。<Link href="/login">登录后继续配置</Link></div> : status.data?.setup_available ? <form action={initialize} className="auth-form"><PasswordField id="setup-token" name="token" label="一次性初始化口令" required autoComplete="off" /><label className="field">管理员名称<input name="display_name" required defaultValue="管理员" /></label><label className="field">邮箱<input name="email" type="email" required autoComplete="username" /></label><PasswordField id="setup-password" name="password" label="密码" required autoComplete="new-password" />{error && <p role="alert" className="form-alert error">{error}</p>}<button className="primary" disabled={busy}>{busy ? "正在初始化…" : "创建管理员并配置平台"}</button></form> : status.data && <p className="platform-alert">请先在部署环境设置初始化口令，再刷新此页面。</p>}
  </AuthShell>;
}
