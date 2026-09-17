
export interface SecurityApproval {
  version?: number;
  approval_id: string;
  run_id: string;
  task_id: string;
  fence_token: number;
  kind: "network" | "tool_effect" | "filesystem" | "command" | "mcp_oauth";
  capability: string;
  target: Record<string, unknown>;
  target_fingerprint: string;
  status: "pending" | "resolved" | "expired" | "consumed";
  decision?: "allow_once" | "allow_run" | "deny";
  reason?: string;
  requested_at: number;
  resolved_at?: number | null;
  expires_at: number;
}

export type EgressRuntimeMode = "manual" | "auto" | "open";

export interface EgressModeState {
  version?: number;
  run_id?: string;
  baseline_mode: string;
  run_setting?: string;
  override?: EgressRuntimeMode | string | null;
  effective_mode: string;
  capped?: boolean;
}

export interface EgressClassification {
  fingerprint?: string;
  capability?: string;
  version?: number;
  domain: string;
  port?: number;
  verdict: "allow" | "ask" | "deny";
  category?: string;
  risk_tags?: string[];
  reason?: string;
  stage_used?: string;
  source?: string;
  task_id?: string;
}

export interface EgressTarget {
  target_id: string;
  target: { domain: string; port: number };
  capability: string;
  version: number;
  decision: "allow_run" | "block_run" | "revoke" | null;
  reason: string;
  updated_at: number;
  policy_denied: boolean;
  classification?: { verdict: "allow" | "ask" | "deny"; reason: string; source: string; classified_at: number } | null;
}

export interface EgressState extends EgressModeState {
  can_resolve?: boolean;
  can_interact?: boolean;
  allowed_modes: EgressRuntimeMode[];
  targets: EgressTarget[];
  target_history?: EgressTarget[];
  records: SecurityApproval[];
  health: { revision?: number; calls_used?: number; remaining_calls?: number; max_calls?: number; degraded?: boolean; reason?: string };
}

