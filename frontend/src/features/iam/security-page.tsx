"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, MonitorSmartphone, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { LoadingSkeleton, MetricStrip } from "@/components/ui/insight";
import { EmptyState, PageHeading } from "@/components/ui/workspace";
import { PasswordField, SessionCard } from "./identity-components";
import { AppShell } from "@/components/app-shell";
import { iamApi } from "@/lib/auth";

export default function AccountSecurityPage() {
  const client = useQueryClient(); const [message, setMessage] = useState("");
  const identity = useQuery({ queryKey: ["identity"], queryFn: iamApi.me });
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: iamApi.sessions });
  const revoke = useMutation({ mutationFn: iamApi.revokeSession, onSuccess: () => client.invalidateQueries({ queryKey: ["sessions"] }) });
  async function change(form: FormData) { setMessage(""); try { await iamApi.changePassword(String(form.get("current_password")), String(form.get("new_password"))); setMessage("密码已更新，全部会话已撤销。请重新登录。"); } catch (cause) { setMessage(cause instanceof Error ? cause.message : "更新失败"); } }
  return <AppShell><div className="page account-page"><PageHeading title="账户与会话" description="检查有效设备、撤销会话，并更新你的登录凭据。" actions={<ShieldCheck size={28} color="var(--cyan)" />} /><MetricStrip label="账户概况" items={[{ label: "账户角色", value: identity.data?.roles.join(" · ") }, { label: "有效会话", value: sessions.data?.filter((item) => !item.is_revoked).length }]} />{(identity.error || sessions.error || revoke.error) && <p role="alert" className="form-alert error">{(identity.error || sessions.error || revoke.error)?.message}</p>}<section className="account-grid"><article className="panel"><div className="panel-header"><h2>身份档案</h2></div><div className="panel-body identity-profile"><b>{identity.data?.display_name || "未命名研究员"}</b><span>{identity.data?.email}</span><div>{identity.data?.roles.map((role) => <i key={role}>{role}</i>)}</div></div></article><article className="panel"><div className="panel-header"><h2><KeyRound size={15} /> 更新密码</h2></div><form action={change} className="panel-body auth-form"><PasswordField label="当前密码" id="current_password" name="current_password" required autoComplete="current-password" /><PasswordField label="新密码" id="new_password" name="new_password" minLength={15} maxLength={128} required autoComplete="new-password" hint="使用 15–128 个字符的长口令。" />{message && <p className="form-alert">{message}</p>}<button className="primary">更新并撤销会话</button></form></article></section><section className="panel session-panel"><div className="panel-header"><h2><MonitorSmartphone size={15} /> 设备会话</h2><button className="secondary" onClick={() => iamApi.logoutAll().then(() => sessions.refetch())}>撤销其他会话</button></div><div className="session-list">{sessions.isPending && <LoadingSkeleton label="正在读取设备会话…" />}{sessions.isSuccess && !sessions.data.length && <EmptyState title="暂无设备会话" />}{sessions.data?.map((item) => <SessionCard key={String(item.id)} item={item} busy={revoke.isPending} onRevoke={(id) => revoke.mutate(id)} />)}</div></section></div></AppShell>;
}
