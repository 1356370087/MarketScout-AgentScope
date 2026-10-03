"use client";

import Link from "next/link";
import { useState } from "react";
import { PasswordField } from "./identity-components";
import { AuthShell } from "@/components/auth-shell";
import { authFetch } from "@/lib/auth";

export default function RegisterPage() {
  const [message, setMessage] = useState(""); const [error, setError] = useState("");
  async function submit(form: FormData) {
    setError("");
    const password = String(form.get("password"));
    if (password !== form.get("confirm")) { setError("两次输入的密码不一致"); return; }
    try {
      await authFetch("/api/auth/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: form.get("email"), display_name: form.get("display_name"), password }) });
      setMessage("验证邮件已发送。完成邮箱验证后，管理员将审核你的研究权限。");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "注册失败"); }
  }
  return <AuthShell eyebrow="NEW RESEARCHER" title="申请研究席位" copy="密码至少 15 个字符。邮箱验证完成后，账户会进入管理员审批队列。" footer={<><span>已有账户？</span><Link href="/login">返回登录</Link></>}>
    {message ? <div className="auth-success"><b>申请已接收</b><p>{message}</p><Link className="secondary" href="/login">返回登录</Link></div> : <form action={submit} className="auth-form">
      <div className="field"><label htmlFor="display_name">显示名称</label><input id="display_name" name="display_name" maxLength={160} autoComplete="name" /></div>
      <div className="field"><label htmlFor="email">工作邮箱</label><input id="email" name="email" type="email" required autoComplete="email" /></div>
      <PasswordField label="密码" id="password" name="password" minLength={15} maxLength={128} required autoComplete="new-password" hint="15–128 个字符，可使用密码管理器生成的长口令。" />
      <PasswordField label="确认密码" id="confirm" name="confirm" minLength={15} maxLength={128} required autoComplete="new-password" />
      {error && <p className="form-alert error" role="alert">{error}</p>}<button className="primary auth-submit">提交申请</button>
    </form>}
  </AuthShell>;
}
