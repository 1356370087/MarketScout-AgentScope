import type { EgressModeState, EgressRuntimeMode, EgressState, EgressTarget, SecurityApproval } from "../contracts/security";
import { apiFetch } from "./http";

export const securityApi = {
  securityApprovals: (runId: string, signal?: AbortSignal) => apiFetch<{ run_id: string; version: number; approvals: SecurityApproval[] }>(`/runs/${encodeURIComponent(runId)}/security-approvals?status=pending`, { signal }),
  resolveSecurityApproval: (runId: string, approvalId: string, decision: "allow_once" | "allow_run" | "deny", reason = "") => apiFetch<SecurityApproval>(`/runs/${encodeURIComponent(runId)}/security-approvals/${encodeURIComponent(approvalId)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, reason }) }),
  egressState: (runId: string, signal?: AbortSignal) => apiFetch<EgressState>(`/runs/${encodeURIComponent(runId)}/egress-state`, { signal }),
  decideEgressTarget: (runId: string, target: EgressTarget, decision: "allow_run" | "block_run" | "revoke", reason = "") => apiFetch<EgressTarget>(`/runs/${encodeURIComponent(runId)}/egress-targets/${target.target_id}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, reason, expected_version: target.version }) }),
  egressMode: (runId: string) => apiFetch<EgressModeState>(`/runs/${encodeURIComponent(runId)}/egress-mode`),
  switchEgressMode: (runId: string, mode: EgressRuntimeMode) => apiFetch<EgressModeState>(`/runs/${encodeURIComponent(runId)}/egress-mode`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) }),
};
