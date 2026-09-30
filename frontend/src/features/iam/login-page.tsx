"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { AuthShell } from "@/components/auth-shell";
import { authFetch, iamApi, localAuthBypass } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(form: FormData) {
    if (localAuthBypass) { router.replace("/research/new"); return; }
    setBusy(true); setError("");
    try {
      await authFetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: form.get("email"), password: form.get("password") }) });
      const user = await iamApi.me();
      router.replace(user.status === "pending_approval" ? "/pending" : "/research/new");
      router.refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "登录失败"); }
    finally { setBusy(false); }
  }
  return <AuthShell eyebrow="继续你的研究" title="欢迎回来" copy="登录 InsightForge，连接问题、证据与洞察。" footer={<><span>尚未注册？</span><Link href="/register">申请研究席位</Link></>}>
    <form action={submit} className="auth-form">
      <div className="field"><label htmlFor="email">工作邮箱</label><input id="email" name="email" type="email" required autoComplete="email" /></div>
      <div className="field"><label htmlFor="password">密码</label><input id="password" name="password" type="password" required autoComplete="current-password" /></div>
      <div className="auth-form-row"><Link href="/forgot-password">忘记密码</Link><span>安全登录</span></div>
      {error && <p className="form-alert error" role="alert">{error}</p>}
      <button className="primary auth-submit" disabled={busy}>{busy ? "正在验证…" : "进入研究台"}</button>
      {localAuthBypass && <p className="dev-badge">LOCAL DEV 已启用，可直接进入</p>}
    </form>
  </AuthShell>;
}
