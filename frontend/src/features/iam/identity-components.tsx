"use client";

import { Check, Eye, EyeOff, MonitorSmartphone, Trash2 } from "lucide-react";
import { useState, type InputHTMLAttributes } from "react";
import { Disclosure, StructuredContent } from "@/components/ui/insight";
import type { Audit, Permission, Role } from "./admin-api";
import "./identity-ui.css";

export function PasswordField({ label, hint, ...props }: InputHTMLAttributes<HTMLInputElement> & { id: string; label: string; hint?: string }) {
  const [visible, setVisible] = useState(false);
  return <div className="field"><label htmlFor={props.id}>{label}</label><div className="password-input"><input {...props} type={visible ? "text" : "password"} aria-describedby={hint ? `${props.id}-hint` : undefined} /><button type="button" aria-label={`${visible ? "隐藏" : "显示"}${label}`} aria-pressed={visible} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={16} /> : <Eye size={16} />}</button></div>{hint && <small id={`${props.id}-hint`}>{hint}</small>}</div>;
}

export function SessionCard({ item, busy, onRevoke }: { item: Record<string, unknown>; busy: boolean; onRevoke: (id: string) => void }) {
  return <article className="identity-session"><span className="identity-session-icon"><MonitorSmartphone size={19} /></span><div><b>{String(item.user_agent || "未知客户端")}</b><span>{String(item.ip_address || "未知地址")} · {new Date(String(item.last_activity_at)).toLocaleString("zh-CN")}</span></div><span className={`session-state ${item.is_current ? "current" : ""}`}>{item.is_revoked ? "已撤销" : item.is_current ? "当前会话" : "有效"}</span>{!item.is_current && !item.is_revoked && <button type="button" disabled={busy} onClick={() => onRevoke(String(item.id))} aria-label="撤销会话"><Trash2 size={15} /></button>}</article>;
}

export function PermissionMatrix({ roles, permissions }: { roles: Role[]; permissions: Permission[] }) {
  return <Disclosure title="权限矩阵" meta={`${roles.length} 个角色 · ${permissions.length} 项权限`}><div className="permission-matrix-scroll" tabIndex={0} role="region" aria-label="角色权限对照"><table className="permission-matrix"><thead><tr><th scope="col">权限</th>{roles.map((role) => <th key={role.id} scope="col">{role.name}</th>)}</tr></thead><tbody>{permissions.map((permission) => <tr key={permission.code}><th scope="row">{permission.name}<small>{permission.domain}</small></th>{roles.map((role) => <td key={role.id}>{role.permission_codes.includes(permission.code) ? <Check size={15} aria-label="具备权限" /> : <span aria-label="无此权限">—</span>}</td>)}</tr>)}</tbody></table></div></Disclosure>;
}

export function AuditItem({ event }: { event: Audit }) {
  return <article className="identity-audit"><header><b>{event.action}</b><time>{new Date(event.created_at).toLocaleString("zh-CN")}</time></header><p>{event.actor_email || "系统"}{event.target_user_id && ` · 成员 ${event.target_user_id}`}</p>{event.detail && Object.keys(event.detail).length > 0 && <Disclosure title="变更详情"><StructuredContent value={event.detail} /></Disclosure>}</article>;
}
