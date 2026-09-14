// Session-level registry of approvals this browser tab already resolved.
// Optimistic removal plus in-flight guards are not enough: a lagging poll or
// an SSE replay of `security.approval.required` can restore a decided card
// after the request finished, and a second click then POSTs an
// already-resolved approval (HTTP 400). Filtering every restore point
// through this set keeps the decision terminal on the client.
export const resolvedSecurityApprovalIds = new Set<string>();
export const resolvedHumanActionIds = new Set<string>();

export function isSecurityApprovalResolved(approvalId: string): boolean {
  return resolvedSecurityApprovalIds.has(approvalId);
}

export function shouldRequestSecurityApprovals(
  requestedRunId: string,
  hydratedRunId: string,
  isHydrated: boolean,
  terminal: boolean,
): boolean {
  return isHydrated && !terminal && requestedRunId === hydratedRunId;
}
