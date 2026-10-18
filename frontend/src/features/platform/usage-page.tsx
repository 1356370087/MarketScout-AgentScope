"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { platformFetch, money, type UserQuotas } from "@/lib/platform";
import { adminApi } from "@/features/iam/admin-api";
import { Heading, Alert, QuotaFields, PlatformModal } from "./ui";
type Usage = { items: Array<{ owner_id: string; period: string; settled: number; reserved: number }>; unknown_operations: number };
export default function UsagePage() {
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ["platform", "usage"], queryFn: () => platformFetch<Usage>("/usage"), refetchInterval: 15000 });
  const users = useQuery({ queryKey: ["admin-users"], queryFn: adminApi.users });
  const [owner, setOwner] = useState("");
  const [editing, setEditing] = useState(false);
  const [limits, setLimits] = useState<UserQuotas>({});
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  async function edit(id: string) { setOwner(id); setError(undefined); try { const result = await platformFetch<{ override: { limits: UserQuotas } | null }>(`/quotas/${id}`); setLimits(result.override?.limits ?? {}); setEditing(true); } catch (cause) { setError(cause); } }
  async function save() { setBusy(true); try { await platformFetch(`/quotas/${owner}`, "PUT", limits); setEditing(false); await cache.invalidateQueries({ queryKey: ["platform"] }); } catch (cause) { setError(cause); } finally { setBusy(false); } }
  return <><Heading title="用量与配额" description="按北京时间自然月统计，费用包含已结算及在途预留。" /><Alert error={error || query.error} /><Alert>{query.data?.unknown_operations ? `有 ${query.data.unknown_operations} 笔调用结果待确认，相关预留继续占用额度。` : null}</Alert>
    <section className="panel platform-panel"><h2>用户月度费用</h2><div className="platform-table-wrap"><table className="platform-table"><thead><tr><th>用户</th><th>月份</th><th>已结算</th><th>在途预留</th><th>操作</th></tr></thead><tbody>{query.data?.items.map((item) => <tr key={`${item.owner_id}:${item.period}`}><td>{users.data?.find((user) => user.id === item.owner_id)?.email ?? item.owner_id}</td><td>{item.period}</td><td>{money(item.settled)}</td><td>{money(item.reserved)}</td><td><button className="secondary" onClick={() => void edit(item.owner_id)}>调整配额</button></td></tr>)}</tbody></table>{query.isSuccess && !query.data.items.length && <div className="platform-empty">尚无月度费用记录</div>}</div></section>
    <section className="panel platform-panel"><h2>为指定用户设置配额</h2><div className="platform-form"><label>选择用户<select value={owner} onChange={(event) => setOwner(event.target.value)}><option value="">请选择</option>{users.data?.map((user) => <option key={user.id} value={user.id}>{user.display_name || user.email}</option>)}</select></label><div><button className="secondary" disabled={!owner} onClick={() => void edit(owner)}>编辑用户覆盖值</button></div></div></section>
    {editing && <PlatformModal title="用户配额覆盖" onClose={() => { if (!busy) setEditing(false); }}><p>留空的项目沿用平台默认值。降低额度后，已结算费用和在途预留继续保留。</p><label><input type="checkbox" checked={limits.monthly_cost_unlimited ?? false} disabled={busy} onChange={(event) => setLimits({ ...limits, monthly_cost_unlimited: event.target.checked })} />月度费用不限额</label><p>不限额时仍记录费用和预留，并执行单次研究上限；取消不限额后恢复下方月度配额。</p><QuotaFields value={limits} onChange={setLimits} disabled={busy} monthlyUnlimited={limits.monthly_cost_unlimited} /><Alert error={error} /><div className="platform-actions"><button className="primary" disabled={busy} onClick={() => void save()}>保存配额</button><button className="secondary" onClick={() => setEditing(false)}>取消</button></div></PlatformModal>}
  </>;
}
